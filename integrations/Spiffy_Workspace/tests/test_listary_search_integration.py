from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
import socket
import threading
import time
import wave

import pytest
from PySide6.QtCore import QEvent, QItemSelectionModel, QObject, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent, QPainter, QPdfWriter
from PySide6.QtNetwork import QLocalServer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QListWidgetItem, QTableWidgetItem

import spiffy_ui.listary_search as listary_search_module
from spiffy_ui.foundation import (
    ActionRegistry,
    CommandPalette,
    set_shared_appearance_profile,
    shared_appearance_profile_id,
)
from spiffy_ui.listary_content_provider import ContentResult, ContentSearchPage, ContentSourceTarget
from spiffy_ui.listary_content_search import (
    MemoryCoreContentResult,
    MemoryCoreContentSearchError,
    MemoryCoreContentSearchPage,
    MemoryCoreContentSourceTarget,
)
from spiffy_ui.listary_search import (
    ListaryAgentClient,
    ListaryAgentError,
    ListaryFilterManagementDialog,
    ListaryFileSearchDialog,
    ListaryFileSearchFacets,
    ListaryFileSearchPage,
    ListarySearchController,
    ListarySearchDialog,
    ListarySearchPage,
    ListarySearchResult,
    _notify_existing_resident,
    _result_icon_name,
    _resident_server_name,
    _send_resident_command,
    create_standalone_dialog,
)
from spiffy_ui.windows_file_manager import ExplorerWindowIdentity, FileManagerActionError, FileManagerActionReceipt
from spiffy_ui.windows_associated_open import (
    ApplicationProcessIdentity,
    ApplicationWindowIdentity,
    AssociatedHandlerIdentity,
    AssociatedOpenError,
    AssociatedOpenReceipt,
    DirectAppLaunchReceipt,
    DirectShortcutLaunchReceipt,
    ShortcutTargetIdentity,
)
from theme.theme_manager import ThemeManager


def _send_key_press(widget, key: Qt.Key, modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> None:
    event = QKeyEvent(QEvent.Type.KeyPress, key, modifiers)
    window = widget.window()
    if hasattr(window, "eventFilter") and window.eventFilter(widget, event):
        return
    QApplication.sendEvent(widget, event)


def _root_record(path: Path, *, root_id: str = "11111111-1111-4111-8111-111111111111") -> dict:
    return {
        "id": root_id,
        "path": str(path),
        "kind": "local",
        "availability": "available",
        "refresh_policy": "automatic",
        "priority": "normal",
        "enabled": True,
        "last_reconciled_unix_ms": None,
        "last_error": None,
    }


def _associated_open_receipt(path: str | Path, *, pid: int = 6202, hwnd: int = 6404) -> AssociatedOpenReceipt:
    target = str(path)
    handler = r"C:\Windows\System32\fixture-handler.exe"
    process = ApplicationProcessIdentity(
        pid,
        303,
        handler,
        r"DESKTOP\fixture",
        (handler, target),
    )
    window = ApplicationWindowIdentity(
        hwnd,
        pid,
        303,
        handler,
        r"DESKTOP\fixture",
        "FixtureWindow",
        (42, hwnd),
    )
    return AssociatedOpenReceipt(
        "spiffy_windows_associated_open_v1",
        "verified",
        target,
        AssociatedHandlerIdentity(handler, "Fixture Handler"),
        process,
        window,
        "process_command_line",
        target,
        2.0,
        4.0,
        True,
    )


def _direct_app_receipt(path: str | Path, *, pid: int = 7202, hwnd: int = 7404) -> DirectAppLaunchReceipt:
    target = str(path)
    process = ApplicationProcessIdentity(
        pid,
        303,
        target,
        r"DESKTOP\fixture",
        (target,),
    )
    window = ApplicationWindowIdentity(
        hwnd,
        pid,
        303,
        target,
        r"DESKTOP\fixture",
        "FixtureWindow",
        (42, hwnd),
    )
    return DirectAppLaunchReceipt(
        "spiffy_windows_direct_app_launch_v1",
        "verified",
        target,
        process,
        window,
        2.0,
        4.0,
        True,
    )


def _direct_shortcut_receipt(
    shortcut: str | Path,
    executable: str | Path,
    *,
    arguments: str = "",
    pid: int = 8202,
    hwnd: int = 8404,
) -> DirectShortcutLaunchReceipt:
    shortcut_path = str(shortcut)
    executable_path = str(executable)
    process = ApplicationProcessIdentity(
        pid,
        303,
        executable_path,
        r"DESKTOP\fixture",
        (executable_path, *([arguments] if arguments else [])),
    )
    window = ApplicationWindowIdentity(
        hwnd,
        pid,
        303,
        executable_path,
        r"DESKTOP\fixture",
        "FixtureWindow",
        (42, hwnd),
    )
    return DirectShortcutLaunchReceipt(
        "spiffy_windows_direct_shortcut_launch_v1",
        "verified",
        shortcut_path,
        ShortcutTargetIdentity(executable_path, arguments, str(Path(executable_path).parent)),
        process,
        window,
        2.0,
        4.0,
        True,
    )


def _drain_root_manager(app: QApplication, manager, timeout_ms: int = 3_000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        assert QThreadPool.globalInstance().waitForDone(1_000) is True
        app.processEvents()
        if not manager._workers:
            return
    raise AssertionError("root manager workers did not drain")


def _serve_once(listener: socket.socket, response_factory, requests: list[dict]) -> threading.Thread:
    def run() -> None:
        connection, _peer = listener.accept()
        with connection:
            reader = connection.makefile("rb")
            request = json.loads(reader.readline().decode("utf-8"))
            requests.append(request)
            response = response_factory(request)
            connection.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _serve_frames(
    listener: socket.socket,
    frame_count: int,
    response_factory,
    requests: list[dict],
    accepts: list[int],
) -> threading.Thread:
    def run() -> None:
        connection, _peer = listener.accept()
        accepts.append(1)
        with connection:
            reader = connection.makefile("rb")
            for _ in range(frame_count):
                request = json.loads(reader.readline().decode("utf-8"))
                requests.append(request)
                response = response_factory(request)
                connection.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_listary_agent_client_uses_authenticated_resident_search_without_opening_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    token = "00000000000000000000000000000001"
    (state / "integration-endpoint-v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protocol_version": 1,
                "process_id": 123,
                "address": f"{host}:{port}",
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    requests: list[dict] = []
    server = _serve_once(
        listener,
        lambda request: {
            "id": request["id"],
            "ok": True,
            "data": {
                "results": [
                    {
                        "item": {
                            "id": 7,
                            "path": str(tmp_path / "Spiffy Clipboard.app"),
                            "parent_path": str(tmp_path),
                            "name": "Spiffy Clipboard.app",
                            "kind": "app",
                            "extension": "app",
                            "size": 2048,
                            "modified_unix_ms": 1760000000000,
                            "available": True,
                            "priority": "high",
                            "preview_hint": "application",
                        },
                        "score": {},
                    }
                ],
                "elapsed_micros": 1250,
                "considered_candidates": 11,
            },
        },
        requests,
    )
    try:
        page = ListaryAgentClient(state, timeout_seconds=1).search(
            "spiffy clipboard", kinds=("app",), request_id=41
        )
    finally:
        server.join(timeout=2)
        listener.close()

    assert len(page.results) == 1
    assert page.results[0].item_id == 7
    assert page.results[0].name == "Spiffy Clipboard.app"
    assert page.results[0].extension == "app"
    assert page.results[0].size_bytes == 2048
    assert page.results[0].modified_unix_ms == 1760000000000
    assert page.results[0].available is True
    assert page.results[0].priority == "high"
    assert page.results[0].preview_hint == "application"
    assert page.elapsed_micros == 1250
    assert requests[0]["token"] == token
    assert requests[0]["method"] == "search"
    assert requests[0]["request_id"] == 41
    assert requests[0]["params"]["kinds"] == ["app"]
    assert requests[0]["params"]["include_unavailable"] is False


def test_listary_agent_client_reuses_one_connection_for_same_thread_requests(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    host, port = listener.getsockname()
    token = "00000000000000000000000000000002"
    (state / "integration-endpoint-v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protocol_version": 1,
                "process_id": 123,
                "address": f"{host}:{port}",
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    requests: list[dict] = []
    accepts: list[int] = []
    server = _serve_frames(
        listener,
        2,
        lambda request: {"id": request["id"], "ok": True, "data": []},
        requests,
        accepts,
    )
    client = ListaryAgentClient(state, timeout_seconds=1)
    try:
        assert client.list_roots() == ()
        assert client.list_roots() == ()
    finally:
        client.close()
        server.join(timeout=2)
        listener.close()

    assert accepts == [1]
    assert [request["method"] for request in requests] == ["list_roots", "list_roots"]


def test_listary_agent_client_saved_filters_use_canonical_rpc_without_local_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    token = "00000000000000000000000000000002"
    (state / "integration-endpoint-v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protocol_version": 1,
                "process_id": 123,
                "address": f"{host}:{port}",
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    preset = {
        "id": "9d36f71f-ef07-4e7d-b29d-ef8375be4e8d",
        "keyword": "review",
        "name": "Review files",
        "extra_query": "ext:txt reviewed",
        "search_paths": [str(tmp_path / "Review")],
        "target": "files",
        "extensions": ["txt"],
    }
    requests: list[dict] = []

    def response(request: dict) -> dict:
        if request["method"] == "list_filters":
            data = [preset]
        elif request["method"] == "put_filter":
            data = preset
        elif request["method"] == "remove_filter":
            data = {"removed": True}
        else:
            raise AssertionError(f"unexpected method: {request['method']}")
        return {"id": request["id"], "ok": True, "data": data}

    server = _serve_frames(listener, 3, response, requests, [])
    client = ListaryAgentClient(state, timeout_seconds=1)
    try:
        assert client.list_filters() == (preset,)
        assert client.put_filter(preset) == preset
        assert client.remove_filter(preset["id"]) is None
    finally:
        client.close()
        server.join(timeout=2)
        listener.close()

    assert [request["method"] for request in requests] == ["list_filters", "put_filter", "remove_filter"]
    assert requests[0]["params"] is None
    assert requests[1]["params"] == preset
    assert requests[2]["params"] == {"filter_id": preset["id"]}
    assert not (state / "settings").exists()


def test_listary_agent_client_saved_filters_reject_malformed_rows_without_fallback(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    token = "00000000000000000000000000000001"
    (state / "integration-endpoint-v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protocol_version": 1,
                "process_id": 123,
                "address": f"{host}:{port}",
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    requests: list[dict] = []
    server = _serve_once(
        listener,
        lambda request: {
            "id": request["id"],
            "ok": True,
            "data": [{"id": "not-a-uuid", "keyword": "bad", "name": "Bad"}],
        },
        requests,
    )
    client = ListaryAgentClient(state, timeout_seconds=1)
    try:
        with pytest.raises(ListaryAgentError) as error:
            client.list_filters()
        assert error.value.code == "invalid_response"
    finally:
        client.close()
        server.join(timeout=2)
        listener.close()
    assert requests[0]["method"] == "list_filters"
    assert not (state / "settings").exists()


def test_listary_agent_client_switches_connection_after_endpoint_rotation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    endpoint_path = state / "integration-endpoint-v1.json"
    client = ListaryAgentClient(state, timeout_seconds=1)
    requests: list[dict] = []

    def run_server(token: str) -> tuple[socket.socket, threading.Thread]:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()
        endpoint_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "protocol_version": 1,
                    "process_id": 123,
                    "address": f"{host}:{port}",
                    "token": token,
                }
            ),
            encoding="utf-8",
        )
        server = _serve_once(
            listener,
            lambda request: {"id": request["id"], "ok": True, "data": []},
            requests,
        )
        return listener, server

    first_listener, first_server = run_server("00000000000000000000000000000001")
    try:
        assert client.list_roots() == ()
    finally:
        first_server.join(timeout=2)
        first_listener.close()

    second_listener, second_server = run_server("00000000000000000000000000000003")
    try:
        assert client.list_roots() == ()
    finally:
        client.close()
        second_server.join(timeout=2)
        second_listener.close()

    assert len(requests) == 2
    assert requests[0]["token"] != requests[1]["token"]


def test_listary_agent_client_cancel_uses_independent_thread_connection(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    host, port = listener.getsockname()
    token = "00000000000000000000000000000001"
    (state / "integration-endpoint-v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protocol_version": 1,
                "process_id": 123,
                "address": f"{host}:{port}",
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    requests: list[dict] = []
    search_received = threading.Event()

    def serve() -> None:
        search_connection, _peer = listener.accept()
        with search_connection:
            search_reader = search_connection.makefile("rb")
            search_request = json.loads(search_reader.readline().decode("utf-8"))
            requests.append(search_request)
            search_received.set()
            cancel_connection, _peer = listener.accept()
            with cancel_connection:
                cancel_reader = cancel_connection.makefile("rb")
                cancel_request = json.loads(cancel_reader.readline().decode("utf-8"))
                requests.append(cancel_request)
                cancel_connection.sendall(
                    json.dumps(
                        {"id": cancel_request["id"], "ok": True, "data": {"cancelled": True}},
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                )
            search_connection.sendall(
                json.dumps(
                    {
                        "id": search_request["id"],
                        "ok": False,
                        "error": {"code": "cancelled", "message": "request cancelled"},
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    client = ListaryAgentClient(state, timeout_seconds=1)
    search_error: list[ListaryAgentError] = []

    def search() -> None:
        try:
            client.search("blocked", request_id=991)
        except ListaryAgentError as exc:
            search_error.append(exc)

    search_thread = threading.Thread(target=search, daemon=True)
    search_thread.start()
    assert search_received.wait(timeout=1)
    try:
        client.cancel(991)
        search_thread.join(timeout=2)
    finally:
        client.close()
        server.join(timeout=2)
        listener.close()

    assert [request["method"] for request in requests] == ["search", "cancel"]
    assert requests[1]["request_id"] == 991
    assert len(search_error) == 1
    assert search_error[0].code == "cancelled"


def test_listary_agent_client_exact_file_search_uses_page_generation_and_sort_contract(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    token = "00000000000000000000000000000003"
    (state / "integration-endpoint-v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protocol_version": 1,
                "process_id": 456,
                "address": f"{host}:{port}",
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    requests: list[dict] = []
    server = _serve_once(
        listener,
        lambda request: {
            "id": request["id"],
            "ok": True,
            "data": {
                "results": [
                    {
                        "item": {
                            "id": 19,
                            "path": str(tmp_path / "scene.mov"),
                            "parent_path": str(tmp_path),
                            "name": "scene.mov",
                            "extension": "mov",
                            "kind": "file",
                            "size": 4096,
                            "modified_unix_ms": 1760000000000,
                            "available": True,
                            "priority": "normal",
                            "preview_hint": "video",
                        },
                        "score": {},
                    }
                ],
                "total_matches": 5012,
                "offset": 200,
                "limit": 100,
                "has_more": True,
                "catalog_generation": 88,
                "considered_candidates": 5012,
                "elapsed_micros": 17250,
                "parsed": {},
                "facets": {
                    "matched_total": 5012,
                    "category_scope_total": 5100,
                    "modified_scope_total": 5050,
                    "categories": [
                        {"category": "folder", "count": 44},
                        {"category": "document", "count": 310},
                        {"category": "picture", "count": 80},
                        {"category": "video", "count": 5012},
                        {"category": "audio", "count": 9},
                        {"category": "other", "count": 12},
                    ],
                    "modified_time": [
                        {"range": "last_1_day", "count": 700},
                        {"range": "last_7_days", "count": 5012},
                        {"range": "last_30_days", "count": 5020},
                        {"range": "last_365_days", "count": 5040},
                        {"range": "older", "count": 10},
                    ],
                    "quick_filters": {
                        "folder": 0,
                        "file": 5012,
                        "pdf": 17,
                        "today": 700,
                        "week": 5012,
                        "large": 88,
                    },
                },
            },
        },
        requests,
    )
    try:
        page = ListaryAgentClient(state, timeout_seconds=1).file_search(
            "scene",
            categories=("video",),
            modified_time="last_7_days",
            offset=200,
            limit=100,
            sort_field="modified",
            sort_direction="ascending",
            expected_catalog_generation=88,
            now_unix_ms=1760000000123,
            timezone_offset_minutes=-240,
            request_id=73,
            include_facets=True,
        )
    finally:
        server.join(timeout=2)
        listener.close()

    assert page.total_matches == 5012
    assert page.offset == 200
    assert page.limit == 100
    assert page.has_more is True
    assert page.catalog_generation == 88
    assert page.considered_candidates == 5012
    assert page.elapsed_micros == 17250
    assert page.results[0].preview_hint == "video"
    assert page.facets is not None
    assert page.facets.category_count("video") == 5012
    assert page.facets.modified_count("last_1_day") == 700
    assert page.facets.quick_count("large") == 88
    assert requests[0]["method"] == "file_search"
    assert requests[0]["request_id"] == 73
    assert requests[0]["params"]["categories"] == ["video"]
    assert requests[0]["params"]["modified_time"] == "last_7_days"
    assert requests[0]["params"]["offset"] == 200
    assert requests[0]["params"]["sort_field"] == "modified"
    assert requests[0]["params"]["sort_direction"] == "ascending"
    assert requests[0]["params"]["expected_catalog_generation"] == 88
    assert requests[0]["params"]["now_unix_ms"] == 1760000000123
    assert requests[0]["params"]["timezone_offset_minutes"] == -240
    assert requests[0]["params"]["include_facets"] is True


def test_listary_agent_client_rejects_non_loopback_endpoint(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "integration-endpoint-v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protocol_version": 1,
                "process_id": 123,
                "address": "192.0.2.20:4242",
                "token": "00000000000000000000000000000001",
            }
        ),
        encoding="utf-8",
    )
    client = ListaryAgentClient(state, timeout_seconds=1)
    try:
        client.search("anything", request_id=1)
    except ListaryAgentError as exc:
        assert exc.code == "endpoint_invalid"
    else:
        raise AssertionError("non-loopback Listary endpoint was accepted")


def test_listary_agent_client_missing_endpoint_is_a_graceful_not_running_state(tmp_path: Path) -> None:
    client = ListaryAgentClient(tmp_path / "missing-state", timeout_seconds=1)
    try:
        client.search("anything", request_id=1)
    except ListaryAgentError as exc:
        assert exc.code == "not_running"
        assert "Open Spiffy Listary" in str(exc)
    else:
        raise AssertionError("missing Listary endpoint was accepted")


def test_listary_agent_client_transport_timeout_has_actionable_recovery_without_socket_details(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    (state / "integration-endpoint-v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protocol_version": 1,
                "process_id": os.getpid(),
                "address": f"127.0.0.1:{port}",
                "token": "00000000000000000000000000000001",
            }
        ),
        encoding="utf-8",
    )

    def stall_once() -> None:
        connection, _ = listener.accept()
        try:
            data = b""
            while b"\n" not in data:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                data += chunk
            time.sleep(0.5)
        finally:
            connection.close()
            listener.close()

    server = threading.Thread(target=stall_once, daemon=True)
    server.start()
    client = ListaryAgentClient(state, timeout_seconds=0.25)
    try:
        with pytest.raises(ListaryAgentError) as captured:
            client.search("anything", request_id=2)
        assert captured.value.code == "unreachable"
        assert "Open Spiffy Listary" in str(captured.value)
        assert "timed out" not in str(captured.value).casefold()
        assert "winerror" not in str(captured.value).casefold()
    finally:
        client.close()
        server.join(timeout=2.0)
    assert not server.is_alive()


def test_listary_dialog_surfaces_missing_resident_without_blocking_ui(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    dialog = ListarySearchDialog(client=ListaryAgentClient(tmp_path / "missing-state", timeout_seconds=1))
    try:
        dialog.search.setText("clip")
        dialog._debounce.stop()
        dialog._start_search()
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()
        assert "Open Spiffy Listary" in dialog.status.text()
        assert dialog.results.count() == 0
        assert dialog.open_button.isEnabled() is False
        assert dialog.reveal_button.isEnabled() is False
        assert dialog.copy_path_button.isEnabled() is False
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_listary_controller_plugs_into_shared_command_palette() -> None:
    app = QApplication.instance() or QApplication([])
    palette = CommandPalette(ActionRegistry())
    controller = ListarySearchController(palette)
    palette.add_provider(controller.palette_items)
    try:
        items = {item.item_id: item for item in palette.all_items()}
        assert "listary:search" in items
        assert "listary:file-search" in items
        assert items["listary:search"].category == "Files & Apps"
        assert items["listary:file-search"].category == "Files & Apps"
        palette.search_box.setText("search apps")
        app.processEvents()
        assert palette.visible_item_ids() == ("listary:search", "listary:file-search")
    finally:
        palette.shutdown()
        palette.deleteLater()


def test_listary_controller_reuses_injected_shared_preview_broker() -> None:
    app = QApplication.instance() or QApplication([])
    palette = CommandPalette(ActionRegistry())
    broker = _PreviewBroker()
    controller = ListarySearchController(palette, preview_broker=broker)
    try:
        controller.open_file_search()
        app.processEvents()
        assert controller._file_dialog is not None
        assert controller._file_dialog._preview_broker is broker
        assert controller._file_dialog._owns_preview_broker is False
        controller.shutdown()
        assert broker.cancelled
    finally:
        palette.shutdown()
        palette.deleteLater()


class _FileSearchClient:
    def __init__(self, pages: list[object], *, filters: Iterable[dict[str, object]] = ()) -> None:
        self.pages = list(pages)
        self.calls: list[dict] = []
        self.cancelled: list[int] = []
        self.activations: list[tuple[int, str]] = []
        self.get_item_calls: list[int] = []
        self.toggle_pin_calls: list[int] = []
        self.favorite_reads = 0
        self.pins: set[int] = set()
        self.toggle_pin_error: BaseException | None = None
        self.favorites_error: BaseException | None = None
        self.filter_reads = 0
        self.list_filters_error: BaseException | None = None
        self.put_filter_error: BaseException | None = None
        self.remove_filter_error: BaseException | None = None
        self.put_filter_error_after_commit = False
        self.remove_filter_error_after_commit = False
        self.put_filter_calls: list[dict[str, object]] = []
        self.remove_filter_calls: list[str] = []
        self.filters = [dict(row) for row in filters]
        self.items: dict[int, ListarySearchResult | None] = {}
        for page in pages:
            if isinstance(page, ListaryFileSearchPage):
                for result in page.results:
                    self.items[result.item_id] = result

    def file_search(self, query: str, **kwargs):
        self.calls.append({"query": query, **kwargs})
        if not self.pages:
            raise AssertionError("unexpected File Search request")
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return page

    def cancel(self, request_id: int) -> None:
        self.cancelled.append(int(request_id))

    def get_item(self, item_id: int) -> ListarySearchResult | None:
        self.get_item_calls.append(int(item_id))
        return self.items.get(int(item_id))

    def record_activation(self, item_id: int, *, source: str = "launcher") -> None:
        self.activations.append((int(item_id), str(source)))

    def get_favorites(self) -> dict:
        self.favorite_reads += 1
        if self.favorites_error is not None:
            raise self.favorites_error
        pins = []
        for item_id in sorted(self.pins):
            result = self.items.get(item_id)
            if result is not None:
                pins.append({"id": f"pin-{item_id}", "title": result.name, "path": result.path})
        return {"items": [], "pins": pins}

    def toggle_pin(self, item_id: int) -> bool:
        item_id = int(item_id)
        self.toggle_pin_calls.append(item_id)
        if item_id in self.pins:
            self.pins.remove(item_id)
            pinned = False
        else:
            self.pins.add(item_id)
            pinned = True
        if self.toggle_pin_error is not None:
            raise self.toggle_pin_error
        return pinned

    def list_filters(self) -> tuple[dict[str, object], ...]:
        self.filter_reads += 1
        if self.list_filters_error is not None:
            raise self.list_filters_error
        return tuple(dict(row) for row in self.filters)

    def put_filter(self, preset: dict[str, object]) -> dict[str, object]:
        payload = dict(preset)
        self.put_filter_calls.append(payload)
        if self.put_filter_error is not None and not self.put_filter_error_after_commit:
            raise self.put_filter_error
        replaced = False
        for index, row in enumerate(self.filters):
            if str(row.get("id") or "") == str(payload.get("id") or ""):
                self.filters[index] = dict(payload)
                replaced = True
                break
        if not replaced:
            self.filters.append(dict(payload))
        if self.put_filter_error is not None:
            raise self.put_filter_error
        return dict(payload)

    def remove_filter(self, filter_id: str) -> None:
        filter_id = str(filter_id)
        self.remove_filter_calls.append(filter_id)
        if self.remove_filter_error is not None and not self.remove_filter_error_after_commit:
            raise self.remove_filter_error
        self.filters = [row for row in self.filters if str(row.get("id") or "") != filter_id]
        if self.remove_filter_error is not None:
            raise self.remove_filter_error


class _RootAwareFileSearchClient(_FileSearchClient):
    def __init__(
        self,
        pages: list[object],
        roots: tuple[dict[str, object], ...],
        *,
        root_error: BaseException | None = None,
    ) -> None:
        super().__init__(pages)
        self.roots = tuple(dict(root) for root in roots)
        self.root_error = root_error
        self.root_reads = 0

    def list_roots(self):
        self.root_reads += 1
        if self.root_error is not None:
            raise self.root_error
        return tuple(dict(root) for root in self.roots)


class _BlockingPinFileSearchClient(_FileSearchClient):
    def __init__(self, pages: list[object]) -> None:
        super().__init__(pages)
        self.pin_started = threading.Event()
        self.pin_release = threading.Event()

    def toggle_pin(self, item_id: int) -> bool:
        item_id = int(item_id)
        self.toggle_pin_calls.append(item_id)
        self.pin_started.set()
        self.pin_release.wait(2.0)
        if item_id in self.pins:
            self.pins.remove(item_id)
            return False
        self.pins.add(item_id)
        return True


class _BlockingQueryFileSearchClient(_FileSearchClient):
    def __init__(self, old_page: ListaryFileSearchPage, new_page: ListaryFileSearchPage) -> None:
        super().__init__([old_page, new_page])
        self.old_page = old_page
        self.new_page = new_page
        self.old_started = threading.Event()
        self.old_release = threading.Event()

    def file_search(self, query: str, **kwargs):
        self.calls.append({"query": query, **kwargs})
        if query == "old":
            self.old_started.set()
            self.old_release.wait(2.0)
            return self.old_page
        if query == "new ext:pdf":
            return self.new_page
        raise AssertionError(f"unexpected File Search query: {query}")


class _BlockingPreviewFileSearchClient(_FileSearchClient):
    def __init__(self, page: ListaryFileSearchPage, blocked_item_id: int) -> None:
        super().__init__([page])
        self.blocked_item_id = int(blocked_item_id)
        self.preview_started = threading.Event()
        self.preview_release = threading.Event()
        self._blocked_once = False

    def get_item(self, item_id: int) -> ListarySearchResult | None:
        item_id = int(item_id)
        self.get_item_calls.append(item_id)
        if item_id == self.blocked_item_id and not self._blocked_once:
            self._blocked_once = True
            self.preview_started.set()
            self.preview_release.wait(2.0)
        return self.items.get(item_id)


class _PreviewBroker(QObject):
    imageReady = Signal(str, QImage)
    failed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, dict]] = []
        self.cancelled: list[str] = []

    def request(self, path: str, **kwargs) -> str:
        key = f"preview-{len(self.requests) + 1}"
        self.requests.append((str(path), dict(kwargs)))
        return key

    def cancel_consumer(self, consumer_id: str) -> None:
        self.cancelled.append(str(consumer_id))


class _ContentSearchProvider:
    display_name = "Fixture Content"

    def __init__(self, results: tuple[MemoryCoreContentResult, ...]) -> None:
        self.results = results
        self.searches: list[tuple[str, int]] = []
        self.resolutions: list[MemoryCoreContentResult] = []
        self.resolve_error: BaseException | None = None
        self.search_error: BaseException | None = None
        self.block_query: str | None = None
        self.block_started = threading.Event()
        self.cancelled_queries: list[str] = []

    def search(self, query: str, *, limit: int, cancel_event: threading.Event) -> MemoryCoreContentSearchPage:
        self.searches.append((str(query), int(limit)))
        if self.search_error is not None:
            raise self.search_error
        if self.block_query == query:
            self.block_started.set()
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and not cancel_event.wait(0.005):
                pass
            if cancel_event.is_set():
                self.cancelled_queries.append(str(query))
                raise MemoryCoreContentSearchError("cancelled", "cancelled")
        return MemoryCoreContentSearchPage(
            self.results,
            3.5,
            "ready" if self.results else "no_results",
            "normal",
            "semantic_not_requested_or_test_provider",
            {
                "writes_search_run": False,
                "writes_search_results": False,
                "writes_explanations": False,
                "scans_folders": False,
                "decodes_media": False,
            },
            "semantic_not_requested_or_test_provider",
            sum(
                token in str(query)
                for token in (
                    "has:transcript",
                    "has:audio",
                    "has:video",
                    "source:voicearchive",
                    "source:spiffycapture",
                    "type:memory_card",
                )
            ),
        )

    def resolve_source(
        self,
        result: MemoryCoreContentResult,
        *,
        cancel_event: threading.Event,
    ) -> MemoryCoreContentSourceTarget:
        self.resolutions.append(result)
        if self.resolve_error is not None:
            raise self.resolve_error
        if cancel_event.is_set():
            raise MemoryCoreContentSearchError("cancelled", "cancelled")
        return MemoryCoreContentSourceTarget(
            path="C:/fixture/current-source.mp4",
            source_state="playable_exact",
            reason_code="registered_original_playable",
            source_sha256="a" * 64,
            target_kind="media_items.original_path",
        )


def _content_result(*, title: str = "Needle result") -> MemoryCoreContentResult:
    return MemoryCoreContentResult(
        document_id="doc-normal",
        doc_id="doc-normal",
        media_id="media-normal",
        title=title,
        snippet="Bounded MemoryCore snippet with the requested needle.",
        doc_type="memory_card",
        source_table="memory_cards",
        source_id="card-normal",
        matched_via=("fts_clean",),
        representation_revision="b" * 64,
        offset_unit="utf8_bytes",
        start_ms=1000,
        end_ms=2000,
    )


def test_deep_file_search_accepts_provider_neutral_content_contract(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([])
    opened: list[str] = []

    class _IndependentContentProvider:
        display_name = "Fixture Provider"

        def search(self, query: str, *, limit: int, cancel_event: threading.Event) -> ContentSearchPage:
            assert query == "needle"
            assert limit == ListaryFileSearchDialog.CONTENT_LIMIT
            assert cancel_event.is_set() is False
            return ContentSearchPage(
                (
                    ContentResult(
                        document_id="fixture-document",
                        doc_id="fixture-document",
                        media_id="fixture-media",
                        title="Provider-neutral result",
                        snippet="Independent provider contract result.",
                        doc_type="text",
                        source_table="fixture_provider",
                        source_id="fixture-source",
                        matched_via=("lexical",),
                        representation_revision="c" * 64,
                        offset_unit="utf8_bytes",
                    ),
                ),
                1.0,
                "ready",
                "local",
                "not_run",
                {},
            )

        def resolve_source(
            self,
            result: ContentResult,
            *,
            cancel_event: threading.Event,
        ) -> ContentSourceTarget:
            assert result.document_id == "fixture-document"
            assert cancel_event.is_set() is False
            return ContentSourceTarget(
                path="C:/fixture/provider-neutral.txt",
                source_state="current",
                reason_code="fixture_current",
                source_sha256="d" * 64,
                target_kind="fixture",
            )

    monkeypatch.setattr(listary_search_module, "open_path", lambda path: opened.append(str(path)) or True)
    dialog = ListaryFileSearchDialog(client=client, content_provider=_IndependentContentProvider())
    try:
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert dialog.results.rowCount() == 1
        assert dialog.results.item(0, 0).text() == "Provider-neutral result"
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._content_action_workers)
        assert opened == ["C:/fixture/provider-neutral.txt"]
        assert client.calls == []
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def _file_result(tmp_path: Path, index: int, *, name: str | None = None) -> ListarySearchResult:
    filename = name or f"scene_{index:03d}.mov"
    return ListarySearchResult(
        item_id=index + 1,
        path=str(tmp_path / filename),
        parent_path=str(tmp_path),
        name=filename,
        kind="file",
        extension="mov",
        size_bytes=4096 + index,
        modified_unix_ms=1760000000000 + index * 1000,
        preview_hint="video",
    )


def _saved_filter(
    *,
    filter_id: str = "9d36f71f-ef07-4e7d-b29d-ef8375be4e8d",
    keyword: str = "review",
    name: str = "Review files",
    extra_query: str = "ext:txt reviewed",
) -> dict[str, object]:
    return {
        "id": filter_id,
        "keyword": keyword,
        "name": name,
        "extra_query": extra_query,
        "search_paths": [],
        "target": "any",
        "extensions": [],
    }


def _facet_projection(
    *,
    matched_total: int,
    category_scope_total: int | None = None,
    modified_scope_total: int | None = None,
    folder: int = 0,
    document: int = 0,
    picture: int = 0,
    video: int = 0,
    audio: int = 0,
    other: int = 0,
    day: int = 0,
    week: int = 0,
    month: int = 0,
    year: int = 0,
    older: int = 0,
    file: int = 0,
    pdf: int = 0,
    large: int = 0,
) -> ListaryFileSearchFacets:
    return ListaryFileSearchFacets(
        matched_total=matched_total,
        category_scope_total=matched_total if category_scope_total is None else category_scope_total,
        modified_scope_total=matched_total if modified_scope_total is None else modified_scope_total,
        categories=(
            ("folder", folder),
            ("document", document),
            ("picture", picture),
            ("video", video),
            ("audio", audio),
            ("other", other),
        ),
        modified_time=(
            ("last_1_day", day),
            ("last_7_days", week),
            ("last_30_days", month),
            ("last_365_days", year),
            ("older", older),
        ),
        quick_filters=(
            ("folder", folder),
            ("file", file),
            ("pdf", pdf),
            ("today", day),
            ("week", week),
            ("large", large),
        ),
    )


def _preview_file_result(
    tmp_path: Path,
    item_id: int,
    *,
    name: str,
    preview_hint: str,
    content: bytes = b"preview fixture",
) -> ListarySearchResult:
    path = tmp_path / name
    path.write_bytes(content)
    stat = path.stat()
    return ListarySearchResult(
        item_id=item_id,
        path=str(path),
        parent_path=str(tmp_path),
        name=name,
        kind="file",
        extension=path.suffix.lstrip("."),
        size_bytes=stat.st_size,
        modified_unix_ms=stat.st_mtime_ns // 1_000_000,
        preview_hint=preview_hint,
        root_id="preview-root",
        stable_file_id=f"preview:{item_id}",
    )


def _wait_file_search(app: QApplication, dialog: ListaryFileSearchDialog, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while (dialog._active_request_id is not None or dialog._workers) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert QThreadPool.globalInstance().waitForDone(max(1, int(timeout * 1000))) is True
    app.processEvents()


def _wait_until(app: QApplication, predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    app.processEvents()
    assert predicate()


def _select_file_search_rows(dialog: ListaryFileSearchDialog, *rows: int) -> None:
    selection_model = dialog.results.selectionModel()
    dialog.results.clearSelection()
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    for row in rows:
        selection_model.select(dialog.results.model().index(row, 0), flags)
    if rows:
        selection_model.setCurrentIndex(
            dialog.results.model().index(rows[-1], 0),
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
    dialog._sync_result_state()


def test_deep_file_search_pages_exact_results_with_generation_fence(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    first_results = tuple(_file_result(tmp_path, index) for index in range(100))
    second_results = tuple(_file_result(tmp_path, 100 + index) for index in range(25))
    client = _FileSearchClient(
        [
            ListaryFileSearchPage(first_results, 125, 0, 100, True, 42, 125, 17500),
            ListaryFileSearchPage(second_results, 125, 100, 100, False, 42, 125, 9100),
        ]
    )
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.search.setText("scene")
        dialog.category.setCurrentIndex(dialog.category.findText("Video"))
        dialog.modified_time.setCurrentIndex(dialog.modified_time.findText("Past 30 Days"))
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert dialog.results.rowCount() == 100
        assert dialog.status.text().startswith("Showing 1-100 of 125 exact matches")
        assert dialog.page_label.text() == "Page 1 of 2"
        assert dialog.next_button.isEnabled() is True
        assert client.calls[0]["categories"] == ("video",)
        assert client.calls[0]["modified_time"] == "last_30_days"
        assert client.calls[0]["offset"] == 0
        assert client.calls[0]["expected_catalog_generation"] is None
        first_now = client.calls[0]["now_unix_ms"]
        first_timezone = client.calls[0]["timezone_offset_minutes"]

        dialog._next_page()
        _wait_file_search(app, dialog)
        assert dialog.results.rowCount() == 25
        assert dialog.status.text().startswith("Showing 101-125 of 125 exact matches")
        assert dialog.page_label.text() == "Page 2 of 2"
        assert dialog.previous_button.isEnabled() is True
        assert dialog.next_button.isEnabled() is False
        assert client.calls[1]["offset"] == 100
        assert client.calls[1]["expected_catalog_generation"] == 42
        assert client.calls[1]["now_unix_ms"] == first_now
        assert client.calls[1]["timezone_offset_minutes"] == first_timezone
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_filter_chips_insert_canonical_query_tokens(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient(
        [ListaryFileSearchPage((), 0, 0, 100, False, 42, 0, 50)]
    )
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.show()
        app.processEvents()
        assert dialog.filter_sidebar.isVisible() is True
        assert dialog.category.isHidden() is True
        assert dialog.modified_time.isHidden() is True
        assert dialog.filter_sidebar.isAncestorOf(dialog.category_reference_panel) is True
        assert dialog.filter_sidebar.isAncestorOf(dialog.search_mode) is True
        assert dialog.filter_sidebar.geometry().left() < dialog.results.geometry().left()
        assert dialog.filter_sidebar.width() == 150
        assert dialog.results.horizontalHeader().sectionSize(0) == 360
        assert 190 <= dialog.preview_panel.width() <= 210
        assert dialog.search.minimumHeight() == 48
        assert dialog.search.placeholderText() == "Search for Files and Folders"
        assert dialog.modified_time_label.text() == "Recently modified"
        assert [button.text() for button in dialog.category_row_buttons] == [
            "Folder", "Document", "Picture", "Video", "Audio", "Other"
        ]
        assert all(not button.icon().isNull() for button in dialog.category_row_buttons)
        assert [button.text() for button in dialog.modified_row_buttons] == [
            "All", "Last 1 day", "Last 7 days", "Last 30 days", "Last 365 days", "Older"
        ]
        assert all(not button.icon().isNull() for button in dialog.modified_row_buttons)
        assert dialog.modified_row_buttons[0].isChecked() is True
        assert dialog.advanced_filter_panel.isHidden() is True
        assert dialog.category_label.isHidden() is True
        assert dialog.advanced_filters_button.text() == ""
        assert dialog.advanced_filters_button.icon().isNull() is False
        assert dialog.clear_filters_button.isHidden() is True
        assert dialog.results.isColumnHidden(2) is True
        assert dialog.results.isColumnHidden(3) is True
        assert dialog.results.isColumnHidden(4) is True
        assert dialog.empty_state_label.isVisible() is True
        assert dialog.empty_state_label.text() == "Begin your search by typing a keyword"
        assert dialog.preview_name.isHidden() is True
        assert dialog.preview_status.isHidden() is True
        assert dialog.preview_button.property("spiffyRole") == "quiet"
        assert dialog.preview_button.isHidden() is True
        assert dialog.copy_path_button.property("spiffyRole") == "quiet"
        assert dialog.copy_path_button.isHidden() is True
        assert dialog.reveal_button.property("spiffyRole") == "quiet"
        assert dialog.reveal_button.isHidden() is True
        assert dialog.open_button.property("spiffyRole") == "primary"
        assert dialog.open_button.isHidden() is True
        labels = [chip.text() for chip in dialog.filter_chips]
        assert labels == ["@Folder", "@File", "@PDF", "@Today", "@Week", "@Large"]
        first_row = dialog.filter_chips[0].parentWidget()
        second_row = dialog.filter_chips[1].parentWidget()
        assert first_row is not None and second_row is not None
        assert second_row.geometry().top() > first_row.geometry().top()
        dialog.search.setFocus()
        QTest.keyClick(dialog.search, Qt.Key.Key_Tab)
        assert dialog.category_row_buttons[0].hasFocus() is True

        dialog.category_row_buttons[1].click()
        dialog.category_row_buttons[2].click()
        assert dialog._selected_file_categories() == ("document", "picture")
        assert dialog.category_row_buttons[1].isChecked() is True
        assert dialog.category_row_buttons[2].isChecked() is True
        dialog.modified_row_buttons[2].click()
        assert dialog.modified_time.currentData() == "last_7_days"
        assert dialog.modified_row_buttons[2].isChecked() is True
        assert dialog.clear_filters_button.isHidden() is False
        assert dialog.clear_filters_button.isVisible() is False

        dialog.advanced_filters_button.click()
        assert dialog.advanced_filter_panel.isVisible() is True
        assert dialog.advanced_filters_button.text() == ""
        assert dialog.clear_filters_button.isHidden() is False
        assert dialog.clear_filters_button.isVisible() is True

        dialog.search.setText("invoice")
        dialog.search.setCursorPosition(len("invoice"))
        dialog._debounce.stop()
        assert dialog.results.isColumnHidden(2) is True
        assert dialog.results.isColumnHidden(3) is True
        assert dialog.results.isColumnHidden(4) is True
        assert dialog.empty_state_label.isHidden() is True
        dialog.filter_chips[2].click()
        assert dialog.search.text() == "invoice ext:pdf"
        assert dialog.search.hasFocus() is True
        assert dialog.filter_chips[2].isChecked() is True
        assert dialog.filter_chips[2].accessibleName() == "Remove active filter: PDF files"

        dialog.search.setCursorPosition(0)
        dialog.filter_chips[3].click()
        assert dialog.search.text() == "date:today invoice ext:pdf"
        assert dialog.filter_chips[3].isChecked() is True

        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert client.calls[0]["query"] == "date:today invoice ext:pdf"
        assert dialog.sidebar_state.text() == "No matching files."
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_saved_presets_apply_discover_and_clear_through_query_owner(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    preset = _saved_filter()
    page = ListaryFileSearchPage((_file_result(tmp_path, 0),), 1, 0, 100, False, 42, 1, 50)
    client = _FileSearchClient([page], filters=[preset])
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.show()
        assert dialog._refresh_saved_filters() is True
        _wait_until(app, lambda: not dialog._saved_filters_loading and not dialog._saved_filter_workers)
        assert client.filter_reads == 1
        assert dialog._saved_filters_supported is True
        assert dialog.advanced_filter_panel.isHidden() is True
        assert dialog.saved_filter_group.isVisible() is False
        dialog.advanced_filters_button.click()
        assert dialog.saved_filter_group.isVisible() is True
        assert dialog.saved_filter_combo.count() == 2
        assert dialog.saved_filter_combo.itemText(1) == "Review files"

        assert dialog._activate_saved_filter(1) is True
        dialog._debounce.stop()
        assert dialog.search.text() == "review:"
        assert dialog.saved_filter_combo.currentIndex() == 0
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert client.calls[-1]["query"] == "review:"

        dialog.search.setText("@rev")
        dialog.search.setCursorPosition(len("@rev"))
        dialog._debounce.stop()
        dialog._update_filter_discovery()
        assert dialog.filter_discovery.isVisible() is True
        assert dialog.filter_discovery.count() == 1
        suggestion = dialog.filter_discovery.item(0)
        assert "@review" in suggestion.text()
        assert suggestion.data(dialog.FILTER_SUGGESTION_ROLE) == "review:"
        assert dialog._accept_filter_discovery() is True
        dialog._debounce.stop()
        assert dialog.search.text() == "review:"

        dialog.search.setText("needle review:")
        dialog._debounce.stop()
        dialog._sync_clear_filters_button()
        assert dialog.clear_filters_button.isEnabled() is True
        assert dialog._clear_current_filters() is True
        dialog._debounce.stop()
        assert dialog.search.text() == "needle"
        assert dialog.search.hasFocus() is True
    finally:
        dialog._debounce.stop()
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_saved_presets_reload_from_core_and_hide_in_content_mode() -> None:
    app = QApplication.instance() or QApplication([])
    review = _saved_filter()
    archive = _saved_filter(
        filter_id="2f932049-766f-45f9-b117-eaf2cbe75d9b",
        keyword="archive",
        name="Archive files",
        extra_query="ext:zip",
    )
    client = _FileSearchClient([], filters=[review])
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.show()
        assert dialog._refresh_saved_filters() is True
        _wait_until(app, lambda: not dialog._saved_filters_loading and not dialog._saved_filter_workers)
        assert [row["keyword"] for row in dialog._saved_filters] == ["review"]
        assert any(choice[1] == "review:" for choice in dialog._filter_choices())

        client.filters = [archive]
        assert dialog._refresh_saved_filters() is True
        _wait_until(app, lambda: not dialog._saved_filters_loading and not dialog._saved_filter_workers)
        assert client.filter_reads == 2
        assert [row["keyword"] for row in dialog._saved_filters] == ["archive"]
        assert dialog.saved_filter_combo.itemText(1) == "Archive files"
        assert any(choice[1] == "archive:" for choice in dialog._filter_choices())
        assert all(choice[1] != "review:" for choice in dialog._filter_choices())

        blocked = dialog.search_mode.blockSignals(True)
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog.search_mode.blockSignals(blocked)
        dialog._apply_search_mode_ui()
        assert dialog.saved_filter_group.isHidden() is True
        assert dialog.category_reference_panel.isHidden() is True
        assert dialog.modified_reference_panel.isHidden() is True
        assert all(choice[1] != "archive:" for choice in dialog._filter_choices())

        blocked = dialog.search_mode.blockSignals(True)
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("files"))
        dialog.search_mode.blockSignals(blocked)
        dialog._apply_search_mode_ui()
        assert dialog.saved_filter_group.isHidden() is False
        assert dialog.advanced_filter_panel.isHidden() is True
        assert dialog.category_reference_panel.isVisible() is True
        assert dialog.modified_reference_panel.isVisible() is True
        assert any(choice[1] == "archive:" for choice in dialog._filter_choices())
        dialog.advanced_filters_button.click()
        assert dialog.saved_filter_group.isVisible() is True
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_old_agent_saved_preset_method_is_nonblocking(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    page = ListaryFileSearchPage((_file_result(tmp_path, 0),), 1, 0, 100, False, 42, 1, 50)
    client = _FileSearchClient([page])
    client.list_filters_error = ListaryAgentError(
        "invalid_request",
        "invalid request: unknown integration method: list_filters",
    )
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.show()
        assert dialog._refresh_saved_filters() is True
        _wait_until(app, lambda: not dialog._saved_filters_loading and not dialog._saved_filter_workers)
        assert dialog._saved_filters_supported is False
        assert dialog.saved_filter_group.isHidden() is True
        assert dialog._saved_filters == ()

        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert client.calls[-1]["query"] == "needle"
        assert dialog.results.rowCount() == 1
        assert dialog.filter_chips[2].isEnabled() is True
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_saved_filter_management_round_trips_core_readback_without_local_store(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    review = _saved_filter()
    client = _FileSearchClient([], filters=[review])
    changes: list[str] = []
    dialog = ListaryFilterManagementDialog(client=client, filters_changed=lambda: changes.append("changed"))
    try:
        dialog.open_manager()
        _wait_until(app, lambda: not dialog._busy and not dialog._workers)
        assert client.filter_reads == 1
        assert dialog.table.rowCount() == 1
        assert dialog.keyword.text() == "review"
        assert dialog.name.text() == "Review files"

        dialog.name.setText("Reviewed deliverables")
        dialog.extra_query.setText("approved final")
        dialog.search_paths.setPlainText(f"{tmp_path / 'A'}\n{tmp_path / 'B'}")
        dialog.target.setCurrentIndex(dialog.target.findData("files"))
        dialog.extensions.setText("mov;mp4")
        dialog._save_current()
        _wait_until(app, lambda: not dialog._busy and not dialog._workers)

        assert len(client.put_filter_calls) == 1
        assert client.filter_reads == 2
        assert client.put_filter_calls[0]["id"] == review["id"]
        assert client.put_filter_calls[0]["name"] == "Reviewed deliverables"
        assert client.put_filter_calls[0]["search_paths"] == [str(tmp_path / "A"), str(tmp_path / "B")]
        assert client.put_filter_calls[0]["target"] == "files"
        assert client.put_filter_calls[0]["extensions"] == ["mov", "mp4"]
        assert dialog.status.text() == "Saved filter."
        assert dialog.table.rowCount() == 1
        assert dialog.name.text() == "Reviewed deliverables"
        assert changes == ["changed"]

        dialog._new_filter()
        new_id = dialog._draft_id
        assert new_id is not None and new_id != review["id"]
        dialog.keyword.setText("plates")
        dialog.name.setText("VFX plates")
        dialog.extra_query.setText("final")
        dialog.target.setCurrentIndex(dialog.target.findData("folders"))
        dialog._save_current()
        _wait_until(app, lambda: not dialog._busy and not dialog._workers)

        assert len(client.put_filter_calls) == 2
        assert client.filter_reads == 3
        assert dialog.table.rowCount() == 2
        assert dialog._draft_id == new_id
        assert dialog.keyword.text() == "plates"
        assert changes == ["changed", "changed"]

        dialog._delete_selected()
        _wait_until(app, lambda: not dialog._busy and not dialog._workers)
        assert client.remove_filter_calls == [new_id]
        assert client.filter_reads == 4
        assert dialog.table.rowCount() == 1
        assert all(str(row.get("id")) != new_id for row in client.filters)
        assert dialog.status.text() == "Deleted saved filter."
        assert changes == ["changed", "changed", "changed"]
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_saved_filter_management_reconciles_ambiguous_mutation_without_retry() -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([], filters=[])
    dialog = ListaryFilterManagementDialog(client=client)
    try:
        dialog.open_manager()
        _wait_until(app, lambda: not dialog._busy and not dialog._workers)
        dialog._new_filter()
        filter_id = dialog._draft_id
        assert filter_id is not None
        dialog.keyword.setText("reconcile")
        dialog.name.setText("Reconcile me")

        client.put_filter_error = TimeoutError("fixture response lost")
        client.put_filter_error_after_commit = True
        dialog._save_current()
        _wait_until(app, lambda: not dialog._busy and not dialog._workers)

        assert len(client.put_filter_calls) == 1
        assert client.filter_reads == 2
        assert any(str(row.get("id")) == filter_id for row in client.filters)
        assert "authoritative readback contains the saved filter" in dialog.status.text()
        assert "No retry was sent" in dialog.status.text()
        assert dialog._requires_reconcile is False

        client.put_filter_error = TimeoutError("fixture response and readback lost")
        client.put_filter_error_after_commit = False
        client.list_filters_error = ConnectionError("fixture readback unavailable")
        dialog.name.setText("Unknown outcome")
        dialog._save_current()
        _wait_until(app, lambda: not dialog._busy and not dialog._workers)

        assert len(client.put_filter_calls) == 2
        assert client.filter_reads == 3
        assert dialog._requires_reconcile is True
        assert dialog.save_button.isEnabled() is False
        assert dialog.delete_button.isEnabled() is False
        assert "No retry was sent" in dialog.status.text()

        client.put_filter_error = None
        client.list_filters_error = None
        dialog.reload()
        _wait_until(app, lambda: not dialog._busy and not dialog._workers)
        assert client.filter_reads == 4
        assert dialog._requires_reconcile is False
        assert dialog.table.rowCount() == 1
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_multi_type_categories_and_clear_all_filters(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    filtered = (_file_result(tmp_path, 0, name="needle-picture.png"),)
    cleared = (
        _file_result(tmp_path, 1, name="needle-document.txt"),
        _file_result(tmp_path, 2, name="needle-picture.png"),
        _file_result(tmp_path, 3, name="needle-audio.wav"),
    )
    client = _FileSearchClient(
        [
            ListaryFileSearchPage(filtered, 1, 0, 100, False, 42, 1, 80),
            ListaryFileSearchPage(cleared, 3, 0, 100, False, 42, 3, 80),
        ]
    )
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.show()
        dialog.search.setText('needle .pdf -.tmp date:today -size:>10mb ".pdf"')
        dialog._debounce.stop()
        dialog._set_selected_file_categories(("document", "picture"), schedule=False)
        dialog.modified_time.setCurrentIndex(dialog.modified_time.findData("last_7_days"))
        dialog._debounce.stop()
        app.processEvents()

        assert dialog._selected_file_categories() == ("document", "picture")
        assert dialog.category.currentIndex() == -1
        assert dialog.category.placeholderText() == "2 Types"
        assert "Documents, Pictures" in dialog.category.accessibleDescription()
        assert dialog.clear_filters_button.isEnabled() is True
        assert dialog.clear_filters_button.isHidden() is False
        assert dialog.clear_filters_button.isVisible() is False
        assert dialog.filter_chips[2].isChecked() is True
        assert dialog.category_row_buttons[1].isChecked() is True
        assert dialog.category_row_buttons[2].isChecked() is True
        assert dialog.modified_row_buttons[2].isChecked() is True

        dialog._start_search()
        _wait_file_search(app, dialog)
        assert client.calls[0]["categories"] == ("document", "picture")
        assert client.calls[0]["modified_time"] == "last_7_days"
        assert client.calls[0]["query"] == 'needle .pdf -.tmp date:today -size:>10mb ".pdf"'

        dialog.clear_filters_button.click()
        dialog._debounce.stop()
        app.processEvents()

        assert dialog._selected_file_categories() == ()
        assert dialog.category.currentIndex() == 0
        assert dialog.category.currentText() == "All Types"
        assert dialog.modified_time.currentData() is None
        assert dialog.search.text() == 'needle ".pdf"'
        assert all(chip.isChecked() is False for chip in dialog.filter_chips)
        assert dialog.clear_filters_button.isEnabled() is False
        assert dialog.clear_filters_button.isHidden() is True
        assert all(button.isChecked() is False for button in dialog.category_row_buttons)
        assert dialog.modified_row_buttons[0].isChecked() is True
        assert dialog.search.hasFocus() is True

        dialog._start_search()
        _wait_file_search(app, dialog)
        assert client.calls[1]["categories"] == ()
        assert client.calls[1]["modified_time"] is None
        assert client.calls[1]["query"] == 'needle ".pdf"'
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_visible_column_headers_use_canonical_sort_owner(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    page = ListaryFileSearchPage((_file_result(tmp_path, 0),), 1, 0, 100, False, 42, 1, 50)
    client = _FileSearchClient([page] * 7)
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.show()
        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        header = dialog.results.horizontalHeader()
        assert dialog.results.isSortingEnabled() is False
        assert header.isSortIndicatorShown() is False

        def click_and_search(column: int, field: str, direction: str) -> None:
            header.sectionClicked.emit(column)
            dialog._debounce.stop()
            assert dialog._offset == 0
            assert dialog._catalog_generation is None
            assert dialog.sort_field.currentData() == field
            assert dialog.sort_direction.currentData() == direction
            assert header.isSortIndicatorShown() is True
            assert header.sortIndicatorSection() == column
            assert header.sortIndicatorOrder() == (
                Qt.SortOrder.AscendingOrder if direction == "ascending" else Qt.SortOrder.DescendingOrder
            )
            assert dialog.results.isSortingEnabled() is False
            dialog._start_search()
            _wait_file_search(app, dialog)
            assert client.calls[-1]["sort_field"] == field
            assert client.calls[-1]["sort_direction"] == direction

        dialog._offset = dialog.PAGE_LIMIT
        dialog._catalog_generation = 42
        click_and_search(0, "name", "ascending")
        click_and_search(0, "name", "descending")
        click_and_search(1, "path", "ascending")
        click_and_search(2, "kind", "ascending")
        click_and_search(3, "modified", "descending")
        click_and_search(4, "size", "descending")

        calls_before_content = len(client.calls)
        generation_before_content = dialog._generation
        blocked = dialog.search_mode.blockSignals(True)
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog.search_mode.blockSignals(blocked)
        dialog._apply_search_mode_ui()
        header.sectionClicked.emit(0)
        app.processEvents()
        assert len(client.calls) == calls_before_content
        assert dialog._generation == generation_before_content
        assert header.isSortIndicatorShown() is False
        assert dialog.results.isSortingEnabled() is False
    finally:
        dialog._debounce.stop()
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_multi_type_popup_toggle_keeps_multiple_categories_active() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = ListaryFileSearchDialog(client=_FileSearchClient([]))
    try:
        dialog.show()
        app.processEvents()
        document_row = dialog.category.findData(("document",))
        picture_row = dialog.category.findData(("picture",))
        assert document_row >= 0 and picture_row >= 0

        assert dialog._toggle_category_filter_row(document_row) is True
        dialog._debounce.stop()
        assert dialog._toggle_category_filter_row(picture_row) is True
        dialog._debounce.stop()
        assert dialog._selected_file_categories() == ("document", "picture")
        assert dialog.category.itemData(document_row, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        assert dialog.category.itemData(picture_row, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        assert dialog.category.itemData(0, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
        assert dialog.category.placeholderText() == "2 Types"

        assert dialog._toggle_category_filter_row(document_row) is True
        dialog._debounce.stop()
        assert dialog._selected_file_categories() == ("picture",)
        assert dialog.category.currentText() == "Pictures"

        assert dialog._toggle_category_filter_row(picture_row) is True
        dialog._debounce.stop()
        assert dialog._selected_file_categories() == ()
        assert dialog.category.currentText() == "All Types"
        assert dialog.category.itemData(0, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_reference_preview_and_actions_shortcuts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    result = _file_result(tmp_path, 0, name="reference-shortcuts.mov")
    client = _FileSearchClient(
        [ListaryFileSearchPage((result,), 1, 0, 100, False, 42, 1, 50)]
    )
    dialog = ListaryFileSearchDialog(client=client)
    opened_menus: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        listary_search_module.QMenu,
        "popup",
        lambda menu, _position: opened_menus.append(
            tuple(action.objectName() for action in menu.actions() if not action.isSeparator())
        ),
    )
    try:
        dialog.show()
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0)
        dialog.results.setFocus()
        app.processEvents()

        preview_width = dialog.results.width()
        assert dialog.preview_panel.isVisible() is True
        assert dialog.preview_button.isChecked() is True
        assert "Alt+P" in dialog.preview_button.toolTip()

        QTest.keyClick(dialog.results, Qt.Key.Key_P, Qt.KeyboardModifier.AltModifier)
        app.processEvents()
        assert dialog.preview_panel.isVisible() is False
        assert dialog.preview_button.isChecked() is False
        assert dialog.results.width() > preview_width
        assert dialog.results.hasFocus() is True

        QTest.keyClick(dialog.results, Qt.Key.Key_P, Qt.KeyboardModifier.AltModifier)
        app.processEvents()
        assert dialog.preview_panel.isVisible() is True
        assert dialog.preview_button.isChecked() is True

        QTest.keyClick(dialog.results, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert opened_menus
        assert opened_menus[-1][:3] == (
            "spiffyFileSearchContextOpen",
            "spiffyFileSearchContextReveal",
            "spiffyFileSearchContextCopyPath",
        )
        assert dialog.results.hasFocus() is True

        menu_count = len(opened_menus)
        dialog.search.setFocus()
        QTest.keyClick(dialog.search, Qt.Key.Key_Right)
        app.processEvents()
        assert len(opened_menus) == menu_count + 1
        assert opened_menus[-1][:3] == (
            "spiffyFileSearchContextOpen",
            "spiffyFileSearchContextReveal",
            "spiffyFileSearchContextCopyPath",
        )
        assert dialog.search.hasFocus() is True

        dialog.results.setFocus()
        QTest.keyClick(dialog.results, Qt.Key.Key_Right)
        app.processEvents()
        assert len(opened_menus) == menu_count + 2
        assert dialog.results.hasFocus() is True

        dialog.search.setFocus()
        dialog.filter_discovery.setVisible(True)
        QTest.keyClick(dialog.search, Qt.Key.Key_Right)
        app.processEvents()
        assert len(opened_menus) == menu_count + 2

        dialog.filter_discovery.setVisible(False)
        dialog.results.clearSelection()
        previous_status = dialog.status.text()
        QTest.keyClick(dialog.search, Qt.Key.Key_Right)
        app.processEvents()
        assert len(opened_menus) == menu_count + 2
        assert dialog.status.text() == previous_status
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_left_rail_renders_same_page_facet_counts_without_changing_tokens(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    results = (
        _file_result(tmp_path, 0, name="needle.pdf"),
        _file_result(tmp_path, 1, name="needle.mov"),
    )
    facets = _facet_projection(
        matched_total=2,
        category_scope_total=7,
        modified_scope_total=5,
        folder=1,
        document=2,
        picture=1,
        video=2,
        audio=0,
        other=1,
        day=2,
        week=4,
        month=5,
        year=5,
        older=0,
        file=2,
        pdf=1,
        large=1,
    )
    client = _FileSearchClient(
        [ListaryFileSearchPage(results, 2, 0, 100, False, 42, 7, 80, facets=facets)]
    )
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.show()
        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert client.calls[0]["include_facets"] is True
        assert [chip.property("filterToken") for chip in dialog.filter_chips] == [
            "folder:",
            "file:",
            "ext:pdf",
            "date:today",
            "date:week",
            "size:>100mb",
        ]
        assert [label.text() for label in dialog.filter_chip_count_labels] == ["1", "2", "1", "2", "4", "1"]
        assert "All 7" in dialog.category_counts_label.text()
        assert "Docs 2" in dialog.category_counts_label.text()
        assert "Any 5" in dialog.modified_counts_label.text()
        assert "7d 4" in dialog.modified_counts_label.text()
        assert [label.text() for label in dialog.category_count_labels] == ["1", "2", "1", "2", "0", "1"]
        assert [label.text() for label in dialog.modified_count_labels] == ["5", "2", "4", "5", "5", "0"]
        assert dialog.category_row_buttons[3].isEnabled() is True
        assert dialog.category_row_buttons[4].isEnabled() is False
        assert dialog.modified_row_buttons[5].isEnabled() is False
        assert dialog.results.verticalHeader().defaultSectionSize() == 30
        assert dialog.sidebar_state.text() == "2 exact matches · live counts"
        assert dialog._last_facet_update_latency_ms is not None
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_typed_at_discovery_replaces_exact_caret_token_and_keyboard_removes() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = ListaryFileSearchDialog(client=_FileSearchClient([]))
    try:
        dialog.show()
        dialog.search.setFocus()
        dialog.search.setText("invoice @p draft")
        dialog._debounce.stop()
        dialog.search.setCursorPosition(len("invoice @p"))
        app.processEvents()

        assert dialog.filter_discovery.isVisible() is True
        assert dialog.filter_discovery.count() == 1
        assert "@PDF" in dialog.filter_discovery.item(0).text()
        assert dialog.filter_discovery.currentRow() == 0

        _send_key_press(dialog.search, Qt.Key.Key_Return)
        dialog._debounce.stop()
        assert dialog.search.text() == "invoice ext:pdf draft"
        assert dialog.search.cursorPosition() == len("invoice ext:pdf")
        assert dialog.filter_discovery.isVisible() is False
        assert dialog.filter_chips[2].isChecked() is True
        assert "Remove active filter" in dialog.filter_chips[2].accessibleName()

        dialog.filter_chips[2].setFocus()
        QTest.keyClick(dialog.filter_chips[2], Qt.Key.Key_Space)
        dialog._debounce.stop()
        assert dialog.search.text() == "invoice draft"
        assert dialog.filter_chips[2].isChecked() is False
        assert dialog.search.hasFocus() is True
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_typed_at_keyboard_navigation_accepts_existing_vocabulary() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = ListaryFileSearchDialog(client=_FileSearchClient([]))
    try:
        dialog.show()
        dialog.search.setFocus()
        dialog.search.setText("@f")
        dialog._debounce.stop()
        dialog.search.setCursorPosition(2)
        app.processEvents()

        assert dialog.filter_discovery.count() == 2
        assert "@Folder" in dialog.filter_discovery.item(0).text()
        assert "@File" in dialog.filter_discovery.item(1).text()
        _send_key_press(dialog.search, Qt.Key.Key_Down)
        assert dialog.filter_discovery.currentRow() == 1
        _send_key_press(dialog.search, Qt.Key.Key_Return)
        dialog._debounce.stop()
        assert dialog.search.text() == "file:"
        assert dialog.filter_chips[1].isChecked() is True
        assert dialog.filter_discovery.isVisible() is False
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_typed_at_unknown_and_quoted_text_stay_literal_and_focus_dismisses() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = ListaryFileSearchDialog(client=_FileSearchClient([]))
    try:
        dialog.show()
        dialog.search.setFocus()
        query = 'report "@PDF" @wat'
        dialog.search.setText(query)
        dialog._debounce.stop()
        dialog.search.setCursorPosition(len(query))
        app.processEvents()

        assert dialog.filter_chips[2].isChecked() is False
        assert dialog.filter_discovery.isVisible() is True
        assert dialog.filter_discovery.count() == 1
        assert "text remains literal" in dialog.filter_discovery.item(0).text()
        assert dialog.filter_discovery.item(0).data(dialog.FILTER_SUGGESTION_ROLE) is None
        assert dialog.search.text() == query

        dialog.category_row_buttons[0].setFocus()
        app.processEvents()
        assert dialog.filter_discovery.isVisible() is False
        assert dialog.search.text() == query

        dialog.search.setText('"@PDF"')
        dialog._debounce.stop()
        dialog.search.setCursorPosition(len('"@PDF"'))
        dialog.search.setFocus()
        app.processEvents()
        assert dialog.filter_discovery.isVisible() is False
        assert dialog.filter_chips[2].isChecked() is False
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_filter_state_is_query_derived_and_discovery_is_ephemeral_on_reopen() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = ListaryFileSearchDialog(client=_FileSearchClient([]))
    try:
        dialog.open_for_query("invoice ext:pdf")
        dialog._debounce.stop()
        app.processEvents()
        assert dialog.filter_chips[2].isChecked() is True
        assert dialog.filter_discovery.isVisible() is False

        dialog.search.setText("invoice @p")
        dialog._debounce.stop()
        dialog.search.setCursorPosition(len("invoice @p"))
        app.processEvents()
        assert dialog.filter_discovery.isVisible() is True
        dialog.reject()
        assert dialog.filter_discovery.isVisible() is False

        dialog.open_for_query("invoice ext:pdf")
        dialog._debounce.stop()
        app.processEvents()
        assert dialog.search.text() == "invoice ext:pdf"
        assert dialog.filter_chips[2].isChecked() is True
        assert dialog.filter_discovery.isVisible() is False

        dialog.reject()
        dialog.open_for_query("")
        dialog._debounce.stop()
        app.processEvents()
        assert dialog.search.text() == ""
        assert dialog.filter_chips[2].isChecked() is False
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_filter_replacement_generation_fences_stale_result(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    old_result = _file_result(tmp_path, 0, name="old.txt")
    new_result = _file_result(tmp_path, 1, name="new.pdf")
    client = _BlockingQueryFileSearchClient(
        ListaryFileSearchPage(
            (old_result,),
            1,
            0,
            100,
            False,
            41,
            1,
            90,
            facets=_facet_projection(matched_total=1, file=1, pdf=0, day=1, week=1),
        ),
        ListaryFileSearchPage(
            (new_result,),
            1,
            0,
            100,
            False,
            42,
            1,
            70,
            facets=_facet_projection(matched_total=1, document=1, file=1, pdf=1, day=1, week=1),
        ),
    )
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.show()
        dialog.search.setFocus()
        dialog.search.setText("old")
        dialog._debounce.stop()
        dialog._start_search()
        assert client.old_started.wait(1.0)

        dialog.search.setText("new @p")
        dialog._debounce.stop()
        dialog.search.setCursorPosition(len("new @p"))
        app.processEvents()
        assert dialog.filter_discovery.isVisible() is True
        _send_key_press(dialog.search, Qt.Key.Key_Return)
        dialog._debounce.stop()
        assert dialog.search.text() == "new ext:pdf"
        dialog._start_search()
        _wait_until(app, lambda: dialog.results.rowCount() == 1 and dialog.results.item(0, 0).text() == "new.pdf")

        client.old_release.set()
        _wait_file_search(app, dialog)
        assert dialog.results.rowCount() == 1
        assert dialog.results.item(0, 0).text() == "new.pdf"
        assert [label.text() for label in dialog.filter_chip_count_labels][:3] == ["0", "1", "1"]
        assert [call["query"] for call in client.calls] == ["old", "new ext:pdf"]
        assert client.cancelled
    finally:
        client.old_release.set()
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_pin_keyboard_context_reorders_reopens_and_unpins(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    alpha = _file_result(tmp_path, 0, name="alpha.mov")
    beta = _file_result(tmp_path, 1, name="beta.mov")
    client = _FileSearchClient(
        [
            ListaryFileSearchPage((alpha, beta), 2, 0, 100, False, 42, 2, 80),
            ListaryFileSearchPage((beta, alpha), 2, 0, 100, False, 42, 2, 75),
            ListaryFileSearchPage((beta, alpha), 2, 0, 100, False, 42, 2, 74),
            ListaryFileSearchPage((alpha, beta), 2, 0, 100, False, 42, 2, 73),
        ]
    )
    recommendation_refreshes: list[str] = []
    dialog = ListaryFileSearchDialog(
        client=client,
        recommendations_changed=lambda: recommendation_refreshes.append("refresh"),
    )
    try:
        dialog.show()
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 1)
        _wait_until(app, lambda: not dialog._pin_state_loading)

        assert dialog.pin_button.text() == "Pin"
        assert dialog.pin_button.isEnabled() is True
        menu = dialog._create_results_context_menu()
        pin_action = next(action for action in menu.actions() if action.objectName() == "spiffyFileSearchContextPin")
        assert pin_action.text() == "Pin"
        assert pin_action.isEnabled() is True
        assert "Ctrl+P" not in pin_action.toolTip()

        dialog.pin_button.click()
        _wait_until(
            app,
            lambda: not dialog._pin_busy and dialog._active_request_id is None and not dialog._pin_state_loading,
        )
        assert client.toggle_pin_calls == [beta.item_id]
        assert client.pins == {beta.item_id}
        assert dialog.results.item(0, 0).text() == beta.name
        assert dialog.pin_button.text() == "Unpin"
        assert dialog.pin_button.isEnabled() is True
        assert dialog._last_pin_receipt is not None
        assert dialog._last_pin_receipt["outcome"] == "success"
        assert recommendation_refreshes == ["refresh"]

        dialog.reject()
        dialog.open_for_query("")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)
        _wait_until(app, lambda: not dialog._pin_state_loading)
        assert dialog.results.item(0, 0).text() == beta.name
        assert dialog.pin_button.text() == "Unpin"

        dialog.pin_button.click()
        _wait_until(
            app,
            lambda: not dialog._pin_busy and dialog._active_request_id is None and not dialog._pin_state_loading,
        )
        assert client.toggle_pin_calls == [beta.item_id, beta.item_id]
        assert client.pins == set()
        assert dialog.results.item(0, 0).text() == alpha.name
        assert dialog.pin_button.text() == "Pin"
        assert recommendation_refreshes == ["refresh", "refresh"]
    finally:
        dialog._debounce.stop()
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_ctrl_p_ctrl_n_navigate_without_toggling_pin(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    first = _file_result(tmp_path, 0, name="nav-one.txt")
    second = _file_result(tmp_path, 1, name="nav-two.txt")
    third = _file_result(tmp_path, 2, name="nav-three.txt")
    client = _FileSearchClient(
        [ListaryFileSearchPage((first, second, third), 3, 0, 100, False, 42, 3, 50)]
    )
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.show()
        dialog._start_search()
        _wait_file_search(app, dialog)
        dialog.search.setFocus()
        dialog._select_result_row(0)
        app.processEvents()

        QTest.keyClick(dialog.search, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert dialog.results.currentRow() == 1
        assert client.toggle_pin_calls == []

        QTest.keyClick(dialog.search, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert dialog.results.currentRow() == 2
        QTest.keyClick(dialog.search, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert dialog.results.currentRow() == 2

        dialog.results.setFocus()
        QTest.keyClick(dialog.results, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert dialog.results.currentRow() == 1
        QTest.keyClick(dialog.results, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert dialog.results.currentRow() == 0
        QTest.keyClick(dialog.results, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert dialog.results.currentRow() == 0
        assert client.toggle_pin_calls == []

        _wait_until(app, lambda: not dialog._pin_state_loading)
        assert dialog.pin_button.isHidden() is True
        assert "Ctrl+P" not in dialog.pin_button.toolTip()
        menu = dialog._create_results_context_menu()
        pin_action = next(action for action in menu.actions() if action.objectName() == "spiffyFileSearchContextPin")
        assert pin_action.isVisible() is True
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_pin_disabled_reason_matches_multi_selection_context_menu(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    first = _file_result(tmp_path, 0)
    second = _file_result(tmp_path, 1)
    client = _FileSearchClient([ListaryFileSearchPage((first, second), 2, 0, 100, False, 42, 2, 60)])
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1)
        app.processEvents()

        assert dialog.pin_button.text() == "Pin / Unpin"
        assert dialog.pin_button.isEnabled() is False
        assert dialog.pin_button.toolTip() == "Pin/Unpin works with one indexed file or folder at a time."
        menu = dialog._create_results_context_menu()
        pin_action = next(action for action in menu.actions() if action.objectName() == "spiffyFileSearchContextPin")
        assert pin_action.isEnabled() is False
        assert pin_action.toolTip() == dialog.pin_button.toolTip()
        assert dialog._toggle_current_pin() is False
        assert dialog.status.text() == dialog.pin_button.toolTip()
        assert client.toggle_pin_calls == []
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_pin_lost_response_reconciles_once_without_replay(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    result = _file_result(tmp_path, 0, name="response_lost.mov")
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 42, 1, 50)])
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.search.setText("response_lost")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)
        _wait_until(app, lambda: not dialog._pin_state_loading)
        client.toggle_pin_error = ListaryAgentError("unreachable", "fixture response lost after dispatch")

        assert dialog._toggle_current_pin() is True
        _wait_until(app, lambda: not dialog._pin_busy)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        assert client.toggle_pin_calls == [result.item_id]
        assert client.pins == {result.item_id}
        assert dialog.pin_button.text() == "Unpin"
        assert dialog.pin_button.isEnabled() is True
        assert dialog._last_pin_receipt is not None
        assert dialog._last_pin_receipt["outcome"] == "reconciled_after_error"
        assert dialog._last_pin_receipt["no_retry"] is True
        assert "No retry was sent" in dialog.status.text()
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_pin_confirmed_response_without_readback_stays_unknown(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    result = _file_result(tmp_path, 0, name="readback_lost.mov")
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 42, 1, 50)])
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.search.setText("readback_lost")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)
        _wait_until(app, lambda: not dialog._pin_state_loading)
        assert dialog.pin_button.isEnabled() is True
        client.favorites_error = ListaryAgentError("unreachable", "fixture readback unavailable")

        assert dialog._toggle_current_pin() is True
        _wait_until(app, lambda: not dialog._pin_busy)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        assert client.toggle_pin_calls == [result.item_id]
        assert client.pins == {result.item_id}
        assert dialog.pin_button.text() == "Pin / Unpin"
        assert dialog.pin_button.isEnabled() is False
        assert dialog._last_pin_receipt is not None
        assert dialog._last_pin_receipt["outcome"] == "unknown"
        assert dialog._last_pin_receipt["no_retry"] is True
        assert "unknown" in dialog.status.text().casefold()
        assert "No retry was sent" in dialog.status.text()
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_pin_stale_completion_cannot_replace_new_selection_state(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    first = _file_result(tmp_path, 0, name="first.mov")
    second = _file_result(tmp_path, 1, name="second.mov")
    client = _BlockingPinFileSearchClient(
        [ListaryFileSearchPage((first, second), 2, 0, 100, False, 42, 2, 50)]
    )
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.search.setText("mov")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)
        _wait_until(app, lambda: not dialog._pin_state_loading)
        assert dialog.pin_button.text() == "Pin"

        assert dialog._toggle_current_pin() is True
        assert client.pin_started.wait(1.0) is True
        _select_file_search_rows(dialog, 1)
        _wait_until(app, lambda: not dialog._pin_state_loading)
        client.pin_release.set()
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        assert client.toggle_pin_calls == [first.item_id]
        assert client.pins == {first.item_id}
        assert dialog._selected_results() == (second,)
        assert dialog._pin_busy is False
        assert dialog._pin_state_item_id == second.item_id
        assert dialog._pin_state is False
        assert dialog.pin_button.text() == "Pin"
        assert dialog.pin_button.isEnabled() is True
    finally:
        client.pin_release.set()
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_content_mode_is_explicit_and_files_never_call_memorycore(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([ListaryFileSearchPage((), 0, 0, 100, False, 42, 0, 50)])
    provider = _ContentSearchProvider((_content_result(),))
    broker = _PreviewBroker()
    dialog = ListaryFileSearchDialog(client=client, preview_broker=broker, content_provider=provider)
    try:
        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert [call["query"] for call in client.calls] == ["needle"]
        assert provider.searches == []

        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert provider.searches == [("needle", dialog.CONTENT_LIMIT)]
        assert [call["query"] for call in client.calls] == ["needle"]
        assert dialog.results.rowCount() == 1
        assert dialog.results.item(0, 0).text() == "Needle result"
        assert dialog.category.isEnabled() is False
        assert dialog.modified_time.isEnabled() is False
        assert dialog.sort_field.isEnabled() is False
        assert dialog.sort_direction.isEnabled() is False
        assert all(chip.isEnabled() is False for chip in dialog.filter_chips)
        assert dialog.page_label.text() == f"Content • top {dialog.CONTENT_LIMIT}"
        assert dialog.quick_filter_label.text() == "Content filters"
        assert dialog.sidebar_state.text() == "1 Content result."
        assert broker.requests == []
        assert [chip.text() for chip in dialog.content_filter_chips] == [
            "@Transcript",
            "@Audio",
            "@Video",
            "@Voice",
            "@Capture",
            "@Memory",
        ]
        assert dialog.file_filter_bar.isHidden() is True
        assert dialog.content_filter_bar.isHidden() is False
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_content_filter_chips_insert_canonical_memorycore_syntax() -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([])
    provider = _ContentSearchProvider((_content_result(),))
    dialog = ListaryFileSearchDialog(client=client, content_provider=provider)
    try:
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog._debounce.stop()
        assert dialog.file_filter_bar.isHidden() is True
        assert dialog.content_filter_bar.isHidden() is False

        dialog.search.setText("needle")
        dialog.search.setCursorPosition(len("needle"))
        dialog.content_filter_chips[2].click()
        assert dialog.search.text() == "needle has:video"
        dialog.content_filter_chips[3].click()
        assert dialog.search.text() == "needle has:video source:voicearchive"
        dialog.content_filter_chips[5].click()
        assert dialog.search.text() == "needle has:video source:voicearchive type:memory_card"

        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert provider.searches[-1] == (
            "needle has:video source:voicearchive type:memory_card",
            dialog.CONTENT_LIMIT,
        )
        assert "3 filters" in dialog.status.text()
        assert client.calls == []

        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("files"))
        dialog._debounce.stop()
        assert dialog.file_filter_bar.isHidden() is False
        assert dialog.content_filter_bar.isHidden() is True
        assert [chip.text() for chip in dialog.filter_chips] == [
            "@Folder", "@File", "@PDF", "@Today", "@Week", "@Large"
        ]
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_content_typed_at_discovery_uses_existing_content_filters() -> None:
    app = QApplication.instance() or QApplication([])
    provider = _ContentSearchProvider((_content_result(),))
    dialog = ListaryFileSearchDialog(client=_FileSearchClient([]), content_provider=provider)
    try:
        dialog.show()
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog._debounce.stop()
        dialog.search.setFocus()
        dialog.search.setText("needle @v")
        dialog._debounce.stop()
        dialog.search.setCursorPosition(len("needle @v"))
        app.processEvents()

        assert dialog.filter_discovery.isVisible() is True
        assert dialog.filter_discovery.count() == 2
        assert "@Video" in dialog.filter_discovery.item(0).text()
        assert "@Voice" in dialog.filter_discovery.item(1).text()
        _send_key_press(dialog.search, Qt.Key.Key_Down)
        _send_key_press(dialog.search, Qt.Key.Key_Return)
        dialog._debounce.stop()
        assert dialog.search.text() == "needle source:voicearchive"
        assert dialog.content_filter_chips[3].isChecked() is True

        dialog._start_search()
        _wait_file_search(app, dialog)
        assert provider.searches[-1] == ("needle source:voicearchive", dialog.CONTENT_LIMIT)
        assert getattr(dialog.client, "calls", []) == []
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_content_query_replacement_cancels_old_generation() -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([])
    provider = _ContentSearchProvider((_content_result(title="Current result"),))
    provider.block_query = "old query"
    dialog = ListaryFileSearchDialog(client=client, content_provider=provider)
    try:
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog.search.setText("old query")
        dialog._debounce.stop()
        dialog._start_search()
        assert provider.block_started.wait(1.0)

        dialog.search.setText("new query")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert provider.cancelled_queries == ["old query"]
        assert [query for query, _limit in provider.searches] == ["old query", "new query"]
        assert dialog.results.rowCount() == 1
        assert dialog.results.item(0, 0).text() == "Current result"
        assert client.calls == []
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_content_actions_revalidate_and_fail_closed(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([])
    provider = _ContentSearchProvider((_content_result(),))
    opened: list[str] = []
    monkeypatch.setattr(listary_search_module, "open_path", lambda path: opened.append(str(path)) or True)
    dialog = ListaryFileSearchDialog(client=client, content_provider=provider)
    try:
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._content_action_workers)
        assert opened == ["C:/fixture/current-source.mp4"]
        assert len(provider.resolutions) == 3
        assert dialog._last_content_action_receipt is not None
        assert dialog._last_content_action_receipt.outcome == "verified"
        assert dialog._last_content_action_receipt.source_sha256 == "a" * 64
        assert client.get_item_calls == []
        assert client.activations == []

        provider.resolve_error = MemoryCoreContentSearchError(
            "source_payload_hash_mismatch",
            "The Content source changed since MemoryCore indexed it.",
        )
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._content_action_workers)
        assert opened == ["C:/fixture/current-source.mp4"]
        assert len(provider.resolutions) == 4
        assert "changed" in dialog.status.text().lower()
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_content_reveal_uses_verified_windows_explorer_owner(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([])
    provider = _ContentSearchProvider((_content_result(),))
    fallback_calls: list[str] = []
    verified_calls: list[tuple[str, str, str]] = []

    def verified(path, *, target_kind, revalidate_target, operation, **_kwargs):
        revalidate_target()
        verified_calls.append((str(path), str(target_kind), str(operation)))
        return FileManagerActionReceipt(
            schema="spiffy_windows_file_manager_action_v1",
            outcome="verified",
            operation=str(operation),
            target_path=str(path),
            target_kind=str(target_kind),
            destination=ExplorerWindowIdentity(
                hwnd=101,
                process_id=202,
                process_start_time_ns=303,
                process_path="C:/Windows/explorer.exe",
                process_username="fixture",
                window_class="CabinetWClass",
                location_path="C:/fixture",
            ),
            window_reused=True,
            location_path_after="C:/fixture",
            selected_paths_after=(str(path),),
            destination_ready_latency_ms=1.0,
            action_latency_ms=2.0,
            foreground_after=True,
        )

    monkeypatch.setattr(listary_search_module.sys, "platform", "win32")
    monkeypatch.setattr(listary_search_module.Path, "is_dir", lambda _self: False)
    monkeypatch.setattr(listary_search_module, "reveal_in_file_manager_verified", verified)
    monkeypatch.setattr(
        listary_search_module,
        "reveal_in_file_manager",
        lambda path: fallback_calls.append(str(path)) or True,
    )
    dialog = ListaryFileSearchDialog(client=client, content_provider=provider)
    try:
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert dialog._reveal_current() is True
        _wait_until(app, lambda: not dialog._content_action_workers)

        assert fallback_calls == []
        assert verified_calls == [("C:/fixture/current-source.mp4", "file", "content_reveal")]
        assert len(provider.resolutions) == 3
        receipt = dialog._last_content_action_receipt
        assert receipt is not None
        assert receipt.outcome == "verified"
        assert receipt.action == "reveal"
        assert receipt.source_sha256 == "a" * 64
        assert receipt.destination is not None
        assert receipt.destination["outcome"] == "verified"
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_content_dispatch_ambiguity_receipt_survives_generation_retirement(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([])
    provider = _ContentSearchProvider((_content_result(),))
    dispatch_started = threading.Event()
    release_dispatch = threading.Event()
    opened: list[str] = []

    def blocking_open(path: str) -> bool:
        opened.append(str(path))
        dispatch_started.set()
        assert release_dispatch.wait(1.0)
        return True

    monkeypatch.setattr(listary_search_module, "open_path", blocking_open)
    dialog = ListaryFileSearchDialog(client=client, content_provider=provider)
    try:
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert dialog._open_current() is True
        deadline = time.monotonic() + 1.0
        while not dispatch_started.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert dispatch_started.is_set()

        dialog._schedule_fresh_search()
        dialog._debounce.stop()
        release_dispatch.set()
        _wait_until(app, lambda: not dialog._content_action_workers)

        assert opened == ["C:/fixture/current-source.mp4"]
        receipt = dialog._last_content_action_receipt
        assert receipt is not None
        assert receipt.outcome == "failed"
        assert receipt.code == "source_changed_after_dispatch"
        assert receipt.action == "open"
        assert receipt.dispatch_happened is True
        assert receipt.ambiguous is True
        assert receipt in dialog._content_action_receipts
    finally:
        release_dispatch.set()
        assert dialog.shutdown() is True
        dialog.deleteLater()


@pytest.mark.parametrize("action", ("open", "reveal", "copy"))
def test_deep_file_search_content_rejects_empty_provider_source_path_without_side_effect(monkeypatch, action: str) -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([])
    native_calls: list[tuple[str, str]] = []

    class _InvalidPathProvider(_ContentSearchProvider):
        def resolve_source(
            self,
            result: MemoryCoreContentResult,
            *,
            cancel_event: threading.Event,
        ) -> ContentSourceTarget:
            self.resolutions.append(result)
            return ContentSourceTarget(
                path="",
                source_state="current",
                reason_code="fixture_current",
                source_sha256="a" * 64,
                target_kind="fixture",
            )

    provider = _InvalidPathProvider((_content_result(),))
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: native_calls.append(("open", str(path))) or True,
    )
    monkeypatch.setattr(
        listary_search_module,
        "reveal_in_file_manager",
        lambda path: native_calls.append(("reveal", str(path))) or True,
    )
    monkeypatch.setattr(
        listary_search_module,
        "reveal_in_file_manager_verified",
        lambda path, **_kwargs: (_ for _ in ()).throw(AssertionError(f"verified reveal called for {path!r}")),
    )
    clipboard = QApplication.clipboard()
    clipboard.setText("sentinel-content-path")
    dialog = ListaryFileSearchDialog(client=client, content_provider=provider)
    try:
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        started = {
            "open": dialog._open_current,
            "reveal": dialog._reveal_current,
            "copy": dialog._copy_current_path,
        }[action]()
        assert started is True
        _wait_until(app, lambda: not dialog._content_action_workers)

        assert native_calls == []
        assert clipboard.text() == "sentinel-content-path"
        assert len(provider.resolutions) == 1
        receipt = dialog._last_content_action_receipt
        assert receipt is not None
        assert receipt.outcome == "failed"
        assert receipt.code == "invalid_source_target"
        assert receipt.action == action
        assert receipt.dispatch_happened is False
        assert receipt.ambiguous is False
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_content_open_revalidates_again_at_dispatch_and_receipts_failure(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([])
    opened: list[str] = []

    class _ChangingContentProvider(_ContentSearchProvider):
        def __init__(self) -> None:
            super().__init__((_content_result(),))
            self.resolve_calls = 0

        def resolve_source(
            self,
            result: MemoryCoreContentResult,
            *,
            cancel_event: threading.Event,
        ) -> MemoryCoreContentSourceTarget:
            self.resolve_calls += 1
            if self.resolve_calls >= 2:
                raise MemoryCoreContentSearchError(
                    "source_payload_hash_mismatch",
                    "The Content source changed after initial verification.",
                )
            return super().resolve_source(result, cancel_event=cancel_event)

    provider = _ChangingContentProvider()
    monkeypatch.setattr(listary_search_module, "open_path", lambda path: opened.append(str(path)) or True)
    dialog = ListaryFileSearchDialog(client=client, content_provider=provider)
    try:
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._content_action_workers)

        assert provider.resolve_calls == 2
        assert opened == []
        receipt = dialog._last_content_action_receipt
        assert receipt is not None
        assert receipt.outcome == "failed"
        assert receipt.code == "source_payload_hash_mismatch"
        assert receipt.action == "open"
        assert receipt.provider_name == "Fixture Content"
        assert receipt.document_id == "doc-normal"
        assert receipt.media_id == "media-normal"
        assert receipt.representation_revision == "b" * 64
        assert receipt.source_sha256 == "a" * 64
        assert receipt.dispatch_happened is False
        assert receipt.ambiguous is False
        assert "changed" in dialog.status.text().lower()
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_quick_switch_forces_files_mode_and_disables_content(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([ListaryFileSearchPage((), 0, 0, 100, False, 42, 0, 50)])
    provider = _ContentSearchProvider((_content_result(),))
    dialog = ListaryFileSearchDialog(client=client, content_provider=provider)
    context = listary_search_module.WindowsFileDialogContext(
        schema="listary_file_context_v1",
        context_token="windows-dialog:content-gate",
        generation=1,
        hwnd=101,
        process_id=202,
        thread_id=303,
        owner_hwnd=404,
        runtime_id=(42, 101),
        window_class="#32770",
        title="Open",
        dialog_kind="open",
        current_directory=str(tmp_path),
        process_start_time_ns=505,
    )
    try:
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog._debounce.stop()
        dialog.set_file_dialog_context(None, context)
        assert dialog.search_mode.currentData() == "files"
        assert dialog.search_mode.isEnabled() is False

        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert provider.searches == []
        assert [call["query"] for call in client.calls] == ["needle"]
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_content_provider_failure_is_graceful_and_files_recovers() -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([ListaryFileSearchPage((), 0, 0, 100, False, 42, 0, 50)])
    provider = _ContentSearchProvider(())
    provider.search_error = MemoryCoreContentSearchError("timeout", "Content search exceeded its bounded execution time.")
    dialog = ListaryFileSearchDialog(client=client, content_provider=provider)
    try:
        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("content"))
        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert dialog.results.rowCount() == 0
        assert "bounded execution time" in dialog.status.text()
        assert dialog.sidebar_state.text() == "Content search unavailable; filters are preserved."
        assert client.calls == []

        dialog.search_mode.setCurrentIndex(dialog.search_mode.findData("files"))
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert [call["query"] for call in client.calls] == ["needle"]
        assert dialog.status.text().startswith("Showing 0 of 0 exact matches")
        assert dialog.sidebar_state.text() == "No matching files."
        assert dialog.category_counts_label.text() == "Counts unavailable for this result snapshot."
        assert [label.text() for label in dialog.filter_chip_count_labels] == ["—"] * 6
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_permission_error_preserves_filters_and_clears_live_counts() -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient(
        [ListaryAgentError("permission_denied", "Indexed root access was denied.")]
    )
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog._set_selected_file_categories(("document",), schedule=False)
        dialog.modified_time.setCurrentIndex(dialog.modified_time.findData("last_7_days"))
        dialog.search.setText("needle ext:pdf")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert dialog.results.rowCount() == 0
        assert "access was denied" in dialog.status.text().lower()
        assert dialog.sidebar_state.text() == "Permission blocked; filters are preserved."
        assert tuple(dialog.category.currentData()) == ("document",)
        assert dialog.modified_time.currentData() == "last_7_days"
        assert dialog.search.text() == "needle ext:pdf"
        assert dialog.category_counts_label.text() == "Counts unavailable."
        assert dialog.modified_counts_label.text() == "Counts unavailable."
        assert [label.text() for label in dialog.filter_chip_count_labels] == ["—"] * 6
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


@pytest.mark.parametrize(
    ("roots", "expected_status", "expected_sidebar", "expected_button", "expected_action"),
    (
        ((), "No indexed folders yet", "No indexed folders yet", "Add folder...", "index"),
        (
            ({"path": r"C:\Unavailable", "enabled": True, "availability": "unavailable"},),
            "disabled or unavailable",
            "disabled or unavailable",
            "Review folders...",
            "manage",
        ),
        (
            ({"path": r"C:\Indexed", "enabled": True, "availability": "available"},),
            "Showing 0 of 0 exact matches",
            "No matching files.",
            None,
            None,
        ),
    ),
    ids=("rootless", "unavailable", "available-no-match"),
)
def test_deep_file_search_empty_catalog_state_exposes_indexed_folder_recovery(
    roots: tuple[dict[str, object], ...],
    expected_status: str,
    expected_sidebar: str,
    expected_button: str | None,
    expected_action: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    page = ListaryFileSearchPage((), 0, 0, 100, False, 7, 0, 75)
    client = _RootAwareFileSearchClient([page], roots)
    dialog = ListaryFileSearchDialog(client=client)
    opened: list[str | None] = []
    monkeypatch.setattr(
        dialog,
        "_open_root_management",
        lambda: opened.append(getattr(dialog, "_root_recovery_action", None)),
        raising=False,
    )
    try:
        dialog.search.setText("missing file")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert dialog.results.rowCount() == 0
        assert expected_status in dialog.status.text()
        assert expected_sidebar in dialog.sidebar_state.text()
        assert client.root_reads == 1
        button = getattr(dialog, "root_recovery_button", None)
        if expected_button is None:
            assert button is None or button.isHidden()
            assert opened == []
        else:
            assert button is not None and not button.isHidden()
            assert button.text() == expected_button
            assert button.accessibleName()
            button.click()
            assert opened == [expected_action]
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_root_status_failure_keeps_no_match_state_fail_closed() -> None:
    app = QApplication.instance() or QApplication([])
    page = ListaryFileSearchPage((), 0, 0, 100, False, 7, 0, 75)
    client = _RootAwareFileSearchClient([page], (), root_error=RuntimeError("root status unavailable"))
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.search.setText("missing file")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert "Showing 0 of 0 exact matches" in dialog.status.text()
        assert dialog.sidebar_state.text() == "No matching files."
        assert client.root_reads == 1
        button = getattr(dialog, "root_recovery_button", None)
        assert button is None or button.isHidden()
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_nonempty_page_does_not_probe_indexed_roots(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    result = _file_result(tmp_path, 0, name="needle.txt")
    page = ListaryFileSearchPage((result,), 1, 0, 100, False, 7, 1, 75)
    client = _RootAwareFileSearchClient([page], ())
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.search.setText("needle")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert dialog.results.rowCount() == 1
        assert client.root_reads == 0
        button = getattr(dialog, "root_recovery_button", None)
        assert button is None or button.isHidden()
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_root_recovery_reuses_parent_quick_switch_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    app = QApplication.instance() or QApplication([])
    client = _QuickSearchClient()
    parent = ListarySearchDialog(client=client)
    dialog = ListaryFileSearchDialog(parent, client=client)
    opened: list[str] = []
    monkeypatch.setattr(parent, "_open_root_management", lambda: opened.append("parent-manager"))
    try:
        dialog._root_recovery_action = "index"
        dialog._activate_root_recovery()
        assert opened == ["parent-manager"]
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()
        parent.reject()
        parent.deleteLater()
        app.processEvents()


def test_deep_file_search_stale_page_restarts_from_first_catalog_generation(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    first = ListaryFileSearchPage(
        (_file_result(tmp_path, 0),),
        101,
        0,
        100,
        True,
        7,
        101,
        1000,
        facets=_facet_projection(matched_total=101, video=101, file=101, week=90),
    )
    refreshed = ListaryFileSearchPage(
        (_file_result(tmp_path, 1, name="new_scene.mov"),),
        1,
        0,
        100,
        False,
        8,
        1,
        800,
        facets=_facet_projection(matched_total=1, video=1, file=1, week=1),
    )
    client = _FileSearchClient(
        [
            first,
            ListaryAgentError("stale_catalog", "catalog generation is stale"),
            refreshed,
        ]
    )
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog.search.setText("scene")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert dialog._catalog_generation == 7

        dialog._next_page()
        deadline = time.monotonic() + 2.0
        while len(client.calls) < 3 and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        _wait_file_search(app, dialog)

        assert len(client.calls) == 3
        assert client.calls[1]["offset"] == 100
        assert client.calls[1]["expected_catalog_generation"] == 7
        assert client.calls[2]["offset"] == 0
        assert client.calls[2]["expected_catalog_generation"] is None
        assert dialog._catalog_generation == 8
        assert dialog.results.item(0, 0).text() == "new_scene.mov"
        assert dialog.page_label.text() == "Page 1 of 1"
        assert [label.text() for label in dialog.filter_chip_count_labels][1] == "1"
        assert dialog.sidebar_state.text() == "1 exact match · live counts"
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_query_edit_retires_old_rows_before_debounce(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    result = _file_result(tmp_path, 0, name="stale.mov")
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 3, 1, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    opened: list[str] = []
    monkeypatch.setattr(listary_search_module, "open_path", lambda path: opened.append(str(path)) or True)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert dialog.results.rowCount() == 1
        assert dialog.open_button.isEnabled() is True
        client.get_item_calls.clear()

        dialog.search.setText("new query")

        assert dialog._debounce.isActive() is True
        assert dialog.results.rowCount() == 0
        assert dialog.open_button.isEnabled() is False
        assert dialog._open_current() is False
        assert opened == []
        assert client.get_item_calls == []
    finally:
        dialog._debounce.stop()
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_open_records_file_search_activation(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    result = _file_result(tmp_path, 0)
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 3, 1, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    opened: list[str] = []

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        opened.append(str(path))
        return _associated_open_receipt(path)

    monkeypatch.setattr(listary_search_module, "open_file_verified", verified)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert opened == [result.path]
        assert client.activations == [(result.item_id, "file_search")]
        assert client.get_item_calls == [result.item_id, result.item_id]
        assert dialog._last_file_manager_receipt is not None
        assert dialog._last_file_manager_receipt.operation == "open_file"
        assert dialog._page_session_valid is False
        assert dialog.next_button.isEnabled() is False
        assert "Refresh required" in dialog.page_label.text()
        assert "refresh before continuing paging" in dialog.status.text()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_direct_executable_app_uses_verified_launch_receipt(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "Fixture Deep App.exe"
    target.write_bytes(b"fixture")
    result = ListarySearchResult(
        item_id=711,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="app",
        root_id="root-deep-app",
        stable_file_id="stable-deep-app",
        available=True,
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 4, 1, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    launched: list[str] = []

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        launched.append(str(path))
        return _direct_app_receipt(path)

    monkeypatch.setattr(listary_search_module, "open_app_verified", verified)
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for direct app {path!r}")),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert launched == [str(target)]
        assert client.activations == [(result.item_id, "file_search")]
        assert client.get_item_calls == [result.item_id, result.item_id]
        receipt = dialog._last_file_manager_receipt
        assert receipt is not None
        assert receipt.operation == "launch_app"
        assert receipt.target_path == str(target)
        assert isinstance(receipt.destination, DirectAppLaunchReceipt)
        assert dialog._page_session_valid is False
        assert "refresh before continuing paging" in dialog.status.text()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_direct_executable_shortcut_uses_verified_launch_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    executable = tmp_path / "Fixture Shortcut Target.exe"
    executable.write_bytes(b"fixture")
    shortcut = tmp_path / "Fixture Shortcut.lnk"
    shortcut.write_bytes(b"fixture shortcut")
    result = ListarySearchResult(
        item_id=712,
        path=str(shortcut),
        parent_path=str(tmp_path),
        name=shortcut.name,
        kind="file",
        extension="lnk",
        root_id="root-shortcut",
        stable_file_id="stable-shortcut",
        available=True,
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 4, 1, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    launched: list[str] = []

    monkeypatch.setattr(
        listary_search_module,
        "inspect_shortcut_target",
        lambda path: ShortcutTargetIdentity(str(executable), "", str(tmp_path)),
    )

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        launched.append(str(path))
        return _direct_shortcut_receipt(path, executable)

    monkeypatch.setattr(listary_search_module, "open_shortcut_verified", verified)
    monkeypatch.setattr(
        listary_search_module,
        "open_file_verified",
        lambda path, **_kwargs: (_ for _ in ()).throw(AssertionError(f"document Open used for shortcut {path!r}")),
    )
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for verifiable shortcut {path!r}")),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert launched == [str(shortcut)]
        assert client.activations == [(result.item_id, "file_search")]
        assert client.get_item_calls == [result.item_id, result.item_id]
        receipt = dialog._last_file_manager_receipt
        assert receipt is not None
        assert receipt.operation == "launch_shortcut"
        assert receipt.target_path == str(shortcut)
        assert isinstance(receipt.destination, DirectShortcutLaunchReceipt)
        assert receipt.destination.executable_path == str(executable)
        assert dialog._page_session_valid is False
        assert "refresh before continuing paging" in dialog.status.text()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_single_folder_open_requires_verified_explorer_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    folder = tmp_path / "Folder Ω with spaces"
    folder.mkdir()
    result = ListarySearchResult(
        item_id=811,
        path=str(folder),
        parent_path=str(tmp_path),
        name=folder.name,
        kind="folder",
        root_id="root-a",
        stable_file_id="stable-folder",
        available=True,
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 3, 1, 100)])
    dialog = ListaryFileSearchDialog(client=client)

    def verified(path, *, revalidate_target, operation, target_kind, **_kwargs):
        revalidate_target()
        destination = ExplorerWindowIdentity(
            141,
            242,
            343,
            r"C:\Windows\explorer.exe",
            r"DESKTOP\fixture",
            "CabinetWClass",
            str(folder),
        )
        return FileManagerActionReceipt(
            "spiffy_windows_file_manager_action_v1",
            "verified",
            operation,
            str(path),
            target_kind,
            destination,
            False,
            str(folder),
            (),
            3.0,
            5.0,
            True,
        )

    monkeypatch.setattr(listary_search_module, "reveal_in_file_manager_verified", verified)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        receipt = dialog._last_file_manager_receipt
        assert receipt is not None
        assert receipt.operation == "open_folder"
        assert receipt.target_path == str(folder)
        assert receipt.destination.destination.hwnd == 141
        assert client.get_item_calls == [result.item_id, result.item_id]
        assert client.activations == [(result.item_id, "file_search")]
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_multi_select_with_folder_fails_closed_before_any_open(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    folder_path = tmp_path / "Folder Ω"
    folder_path.mkdir()
    file_result = _file_result(tmp_path, 0, name="clip.mov")
    folder_result = ListarySearchResult(
        item_id=812,
        path=str(folder_path),
        parent_path=str(tmp_path),
        name=folder_path.name,
        kind="folder",
        root_id="root-a",
        stable_file_id="stable-folder",
        available=True,
    )
    results = (file_result, folder_result)
    client = _FileSearchClient([ListaryFileSearchPage(results, 2, 0, 100, False, 3, 2, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    opened: list[str] = []
    verified: list[str] = []
    monkeypatch.setattr(listary_search_module, "open_path", lambda path: opened.append(str(path)) or True)
    monkeypatch.setattr(
        listary_search_module,
        "reveal_in_file_manager_verified",
        lambda path, **_kwargs: verified.append(str(path)),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1)

        assert dialog.open_button.isEnabled() is False
        assert dialog.copy_path_button.isEnabled() is True
        assert "one verified Explorer destination at a time" in dialog.details.text()
        assert dialog._open_current() is False
        assert opened == []
        assert verified == []
        assert client.get_item_calls == []
        assert client.activations == []
        assert "one at a time" in dialog.status.text().lower()
        assert "semantic readback" in dialog.status.text().lower()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_multi_select_with_shortcut_fails_closed_before_any_open(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    shortcut = tmp_path / "Fixture Multi Shortcut.lnk"
    shortcut.write_bytes(b"fixture shortcut")
    file_result = _file_result(tmp_path, 0, name="clip.mov")
    shortcut_result = ListarySearchResult(
        item_id=813,
        path=str(shortcut),
        parent_path=str(tmp_path),
        name=shortcut.name,
        kind="file",
        extension="lnk",
        root_id="root-a",
        stable_file_id="stable-shortcut",
        available=True,
    )
    results = (file_result, shortcut_result)
    client = _FileSearchClient([ListaryFileSearchPage(results, 2, 0, 100, False, 3, 2, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    opened: list[str] = []
    monkeypatch.setattr(listary_search_module, "open_file_verified", lambda path, **_kwargs: opened.append(str(path)))
    monkeypatch.setattr(listary_search_module, "open_shortcut_verified", lambda path, **_kwargs: opened.append(str(path)))
    monkeypatch.setattr(listary_search_module, "open_path", lambda path: opened.append(str(path)) or True)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1)

        assert dialog.open_button.isEnabled() is False
        assert dialog.copy_path_button.isEnabled() is True
        assert "shortcut batch Open requires every selected row" in dialog.details.text()
        assert dialog._open_current() is False
        assert opened == []
        assert client.get_item_calls == []
        assert client.activations == []
        assert "every selected row" in dialog.status.text().lower()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_multi_select_direct_shortcuts_launch_in_selection_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    executable = tmp_path / "Fixture Batch Shortcut Target.exe"
    executable.write_bytes(b"fixture")
    results = tuple(
        ListarySearchResult(
            item_id=860 + index,
            path=str(tmp_path / f"Fixture Batch Shortcut {index + 1}.lnk"),
            parent_path=str(tmp_path),
            name=f"Fixture Batch Shortcut {index + 1}.lnk",
            kind="file",
            extension="lnk",
            root_id="root-batch-shortcut",
            stable_file_id=f"stable-batch-shortcut-{index + 1}",
            available=True,
        )
        for index in range(3)
    )
    for result in results:
        Path(result.path).write_bytes(b"fixture shortcut")
    client = _FileSearchClient([ListaryFileSearchPage(results, 3, 0, 100, False, 12, 3, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    launched: list[str] = []

    monkeypatch.setattr(
        listary_search_module,
        "inspect_shortcut_target",
        lambda path: ShortcutTargetIdentity(str(executable), "", str(tmp_path)),
    )

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        launched.append(str(path))
        index = len(launched)
        return _direct_shortcut_receipt(path, executable, pid=8600 + index, hwnd=8800 + index)

    monkeypatch.setattr(listary_search_module, "open_shortcut_verified", verified)
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for direct shortcut {path!r}")),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 2)

        assert dialog.open_button.isEnabled() is True
        assert "verified executable shortcut launches" in dialog.details.text().lower()
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        assert launched == [results[0].path, results[2].path]
        assert client.activations == [
            (results[0].item_id, "file_search"),
            (results[2].item_id, "file_search"),
        ]
        batch = dialog._last_file_manager_batch_receipt
        assert batch is not None
        assert batch.operation == "launch_shortcut_batch"
        assert batch.outcome == "verified"
        assert batch.requested_item_ids == (results[0].item_id, results[2].item_id)
        assert tuple(receipt.operation for receipt in batch.completed) == ("launch_shortcut", "launch_shortcut")
        assert tuple(receipt.item_id for receipt in batch.completed) == (
            results[0].item_id,
            results[2].item_id,
        )
        assert dialog._last_file_manager_failure_receipt is None
        assert dialog._page_session_valid is False
        assert dialog.status.text().startswith("Verified launch for 2 selected shortcuts.")
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_mixed_verified_batch_uses_each_existing_delivery_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    file_result = _file_result(tmp_path, 0, name="Mixed Verified Clip.mov")
    Path(file_result.path).write_bytes(b"fixture media")
    app_path = tmp_path / "Mixed Verified Tool.exe"
    app_path.write_bytes(b"fixture executable")
    app_result = ListarySearchResult(
        item_id=871,
        path=str(app_path),
        parent_path=str(tmp_path),
        name=app_path.name,
        kind="app",
        extension="exe",
        root_id="root-mixed-verified",
        stable_file_id="stable-mixed-app",
        available=True,
    )
    shortcut_path = tmp_path / "Mixed Verified Shortcut.lnk"
    shortcut_path.write_bytes(b"fixture shortcut")
    shortcut_result = ListarySearchResult(
        item_id=872,
        path=str(shortcut_path),
        parent_path=str(tmp_path),
        name=shortcut_path.name,
        kind="file",
        extension="lnk",
        root_id="root-mixed-verified",
        stable_file_id="stable-mixed-shortcut",
        available=True,
    )
    results = (file_result, app_result, shortcut_result)
    client = _FileSearchClient([ListaryFileSearchPage(results, 3, 0, 100, False, 12, 3, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    attempts: list[tuple[str, str]] = []

    monkeypatch.setattr(
        listary_search_module,
        "inspect_shortcut_target",
        lambda path: ShortcutTargetIdentity(str(app_path), "", str(tmp_path)),
    )

    def open_file(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        attempts.append(("open_file", str(path)))
        return _associated_open_receipt(path, pid=8711, hwnd=8811)

    def launch_app(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        attempts.append(("launch_app", str(path)))
        return _direct_app_receipt(path, pid=8712, hwnd=8812)

    def launch_shortcut(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        attempts.append(("launch_shortcut", str(path)))
        return _direct_shortcut_receipt(path, app_path, pid=8713, hwnd=8813)

    monkeypatch.setattr(listary_search_module, "open_file_verified", open_file)
    monkeypatch.setattr(listary_search_module, "open_app_verified", launch_app)
    monkeypatch.setattr(listary_search_module, "open_shortcut_verified", launch_shortcut)
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for mixed verified item {path!r}")),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1, 2)

        assert dialog.open_button.isEnabled() is True
        assert "mixed verified" in dialog.details.text().casefold()
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        assert attempts == [
            ("open_file", file_result.path),
            ("launch_app", app_result.path),
            ("launch_shortcut", shortcut_result.path),
        ]
        assert client.activations == [
            (file_result.item_id, "file_search"),
            (app_result.item_id, "file_search"),
            (shortcut_result.item_id, "file_search"),
        ]
        batch = dialog._last_file_manager_batch_receipt
        assert batch is not None
        assert batch.operation == "mixed_verified_batch"
        assert batch.outcome == "verified"
        assert tuple(receipt.operation for receipt in batch.completed) == (
            "open_file",
            "launch_app",
            "launch_shortcut",
        )
        assert dialog._last_file_manager_failure_receipt is None
        assert dialog._page_session_valid is False
        assert dialog.status.text().startswith("Verified 3 mixed selected destinations.")
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_mixed_verified_batch_revalidates_all_before_first_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    file_result = _file_result(tmp_path, 0, name="Atomic Mixed Clip.mov")
    Path(file_result.path).write_bytes(b"fixture media")
    app_path = tmp_path / "Atomic Mixed Tool.exe"
    app_path.write_bytes(b"fixture executable")
    app_result = ListarySearchResult(
        item_id=873,
        path=str(app_path),
        parent_path=str(tmp_path),
        name=app_path.name,
        kind="app",
        extension="exe",
        root_id="root-atomic-mixed",
        stable_file_id="stable-atomic-mixed-app",
        available=True,
    )
    results = (file_result, app_result)
    client = _FileSearchClient([ListaryFileSearchPage(results, 2, 0, 100, False, 12, 2, 100)])
    client.items[app_result.item_id] = None
    dialog = ListaryFileSearchDialog(client=client)
    attempts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        listary_search_module,
        "open_file_verified",
        lambda path, **_kwargs: attempts.append(("open_file", str(path))) or _associated_open_receipt(path),
    )
    monkeypatch.setattr(
        listary_search_module,
        "open_app_verified",
        lambda path, **_kwargs: attempts.append(("launch_app", str(path))) or _direct_app_receipt(path),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1)

        assert dialog.open_button.isEnabled() is True
        assert dialog._open_current() is False
        assert attempts == []
        assert client.activations == []
        assert "no longer exists" in dialog.status.text()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_mixed_verified_batch_stops_after_ambiguous_destination(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    file_result = _file_result(tmp_path, 0, name="Partial Mixed Clip.mov")
    Path(file_result.path).write_bytes(b"fixture media")
    app_path = tmp_path / "Partial Mixed Tool.exe"
    app_path.write_bytes(b"fixture executable")
    app_result = ListarySearchResult(
        item_id=874,
        path=str(app_path),
        parent_path=str(tmp_path),
        name=app_path.name,
        kind="app",
        extension="exe",
        root_id="root-partial-mixed",
        stable_file_id="stable-partial-mixed-app",
        available=True,
    )
    shortcut_path = tmp_path / "Partial Mixed Shortcut.lnk"
    shortcut_path.write_bytes(b"fixture shortcut")
    shortcut_result = ListarySearchResult(
        item_id=875,
        path=str(shortcut_path),
        parent_path=str(tmp_path),
        name=shortcut_path.name,
        kind="file",
        extension="lnk",
        root_id="root-partial-mixed",
        stable_file_id="stable-partial-mixed-shortcut",
        available=True,
    )
    results = (file_result, app_result, shortcut_result)
    client = _FileSearchClient([ListaryFileSearchPage(results, 3, 0, 100, False, 12, 3, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    attempts: list[tuple[str, str]] = []

    monkeypatch.setattr(
        listary_search_module,
        "inspect_shortcut_target",
        lambda path: ShortcutTargetIdentity(str(app_path), "", str(tmp_path)),
    )

    def open_file(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        attempts.append(("open_file", str(path)))
        return _associated_open_receipt(path, pid=8741, hwnd=8841)

    def launch_app(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        attempts.append(("launch_app", str(path)))
        destination = _direct_app_receipt(path, pid=8742, hwnd=8842)
        raise AssociatedOpenError(
            "delivery_unverifiable",
            "fixture mixed app accepted launch without final exact readback",
            ambiguous=True,
            dispatch_happened=True,
            destination_process=destination.destination_process,
            destination_window=destination.destination_window,
        )

    monkeypatch.setattr(listary_search_module, "open_file_verified", open_file)
    monkeypatch.setattr(listary_search_module, "open_app_verified", launch_app)
    monkeypatch.setattr(
        listary_search_module,
        "open_shortcut_verified",
        lambda path, **_kwargs: attempts.append(("launch_shortcut", str(path)))
        or _direct_shortcut_receipt(path, app_path),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1, 2)

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        assert attempts == [
            ("open_file", file_result.path),
            ("launch_app", app_result.path),
        ]
        assert client.activations == [(file_result.item_id, "file_search")]
        batch = dialog._last_file_manager_batch_receipt
        assert batch is not None
        assert batch.operation == "mixed_verified_batch"
        assert batch.outcome == "partial"
        assert tuple(receipt.item_id for receipt in batch.completed) == (file_result.item_id,)
        failure = dialog._last_file_manager_failure_receipt
        assert failure is not None
        assert failure.code == "delivery_unverifiable"
        assert failure.ambiguous is True
        assert failure.dispatch_happened is True
        assert dialog._page_session_valid is False
        assert dialog.status.text().startswith("Verified 1 of 3 mixed selected destinations; stopped")
        assert "no retry" in dialog.status.text().casefold()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_mixed_batch_never_promotes_unverified_video_workspace_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    file_result = _file_result(tmp_path, 0, name="Video Workspace Guard.mov")
    Path(file_result.path).write_bytes(b"fixture media")
    launch_path = (tmp_path / "Launch Spiffy Workspace.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    synthetic = (
        tmp_path
        / "providers"
        / "workspace_catalog"
        / "ss-scripts"
        / "spiffy_workspace"
        / "spiffy_video_editor.workspaceapp"
    )
    video_result = ListarySearchResult(
        item_id=876,
        path=str(synthetic),
        parent_path=str(synthetic.parent),
        name=synthetic.name,
        kind="app",
        preview_hint=_workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_video_editor",
            label="Spiffy Video Editor",
            aliases=["Video Editor"],
            launch_path=str(launch_path),
        ),
    )
    results = (file_result, video_result)
    client = _FileSearchClient([ListaryFileSearchPage(results, 2, 0, 100, False, 12, 2, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    opened: list[str] = []
    monkeypatch.setattr(listary_search_module, "open_path", lambda path: opened.append(str(path)) or True)
    monkeypatch.setattr(
        listary_search_module,
        "open_file_verified",
        lambda path, **_kwargs: opened.append(str(path)) or _associated_open_receipt(path),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1)

        assert dialog.open_button.isEnabled() is False
        assert "verified windows batch actions" in dialog.details.text().casefold()
        assert dialog._open_current() is False
        assert opened == []
        assert client.activations == []
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_shortcut_batch_revalidation_is_atomic_before_launch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    executable = tmp_path / "Atomic Shortcut Target.exe"
    executable.write_bytes(b"fixture")
    results = tuple(
        ListarySearchResult(
            item_id=870 + index,
            path=str(tmp_path / f"Atomic Batch Shortcut {index + 1}.lnk"),
            parent_path=str(tmp_path),
            name=f"Atomic Batch Shortcut {index + 1}.lnk",
            kind="file",
            extension="lnk",
            root_id="root-atomic-shortcut",
            stable_file_id=f"stable-atomic-shortcut-{index + 1}",
            available=True,
        )
        for index in range(2)
    )
    for result in results:
        Path(result.path).write_bytes(b"fixture shortcut")
    client = _FileSearchClient([ListaryFileSearchPage(results, 2, 0, 100, False, 13, 2, 100)])
    client.items[results[1].item_id] = None
    dialog = ListaryFileSearchDialog(client=client)
    launched: list[str] = []
    monkeypatch.setattr(
        listary_search_module,
        "inspect_shortcut_target",
        lambda path: ShortcutTargetIdentity(str(executable), "", str(tmp_path)),
    )
    monkeypatch.setattr(
        listary_search_module,
        "open_shortcut_verified",
        lambda path, **_kwargs: launched.append(str(path)) or _direct_shortcut_receipt(path, executable),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1)

        assert dialog.open_button.isEnabled() is True
        assert dialog._open_current() is False
        assert launched == []
        assert client.activations == []
        assert "no longer exists" in dialog.status.text()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_partial_shortcut_batch_stops_and_records_only_verified_launches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    executable = tmp_path / "Partial Shortcut Target.exe"
    executable.write_bytes(b"fixture")
    results = tuple(
        ListarySearchResult(
            item_id=880 + index,
            path=str(tmp_path / f"Partial Batch Shortcut {index + 1}.lnk"),
            parent_path=str(tmp_path),
            name=f"Partial Batch Shortcut {index + 1}.lnk",
            kind="file",
            extension="lnk",
            root_id="root-partial-shortcut",
            stable_file_id=f"stable-partial-shortcut-{index + 1}",
            available=True,
        )
        for index in range(3)
    )
    for result in results:
        Path(result.path).write_bytes(b"fixture shortcut")
    client = _FileSearchClient([ListaryFileSearchPage(results, 3, 0, 100, False, 14, 3, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    attempts: list[str] = []
    monkeypatch.setattr(
        listary_search_module,
        "inspect_shortcut_target",
        lambda path: ShortcutTargetIdentity(str(executable), "", str(tmp_path)),
    )

    def launch_until_second(path, *, revalidate_target, **_kwargs):
        attempts.append(str(path))
        revalidate_target()
        if str(path) == results[1].path:
            destination = _direct_shortcut_receipt(path, executable, pid=10802, hwnd=11004)
            raise AssociatedOpenError(
                "delivery_unverifiable",
                "fixture shortcut accepted the second launch without exact readback",
                ambiguous=True,
                dispatch_happened=True,
                destination_process=destination.destination_process,
                destination_window=destination.destination_window,
            )
        return _direct_shortcut_receipt(path, executable, pid=10801, hwnd=11003)

    monkeypatch.setattr(listary_search_module, "open_shortcut_verified", launch_until_second)
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for direct shortcut {path!r}")),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1, 2)

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        assert attempts == [results[0].path, results[1].path]
        assert client.activations == [(results[0].item_id, "file_search")]
        batch = dialog._last_file_manager_batch_receipt
        assert batch is not None
        assert batch.operation == "launch_shortcut_batch"
        assert batch.outcome == "partial"
        assert tuple(receipt.item_id for receipt in batch.completed) == (results[0].item_id,)
        failure = dialog._last_file_manager_failure_receipt
        assert failure is not None
        assert failure.code == "delivery_unverifiable"
        assert failure.ambiguous is True
        assert failure.dispatch_happened is True
        assert dialog._page_session_valid is False
        assert dialog.status.text().startswith("Verified 1 of 3 selected shortcut launches; stopped")
        assert "no retry" in dialog.status.text().casefold()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_revalidation_uses_fresh_path_for_same_identity_move(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    result = _file_result(tmp_path, 0, name="before.mov")
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 3, 1, 100)])
    client.items[result.item_id] = ListarySearchResult(
        item_id=result.item_id,
        path=str(tmp_path / "after.mov"),
        parent_path=str(tmp_path),
        name="after.mov",
        kind="file",
        root_id=result.root_id,
        stable_file_id=result.stable_file_id,
        extension="mov",
        size_bytes=result.size_bytes,
        modified_unix_ms=result.modified_unix_ms,
        available=True,
        priority=result.priority,
        preview_hint=result.preview_hint,
    )
    dialog = ListaryFileSearchDialog(client=client)
    opened: list[str] = []

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        opened.append(str(path))
        return _associated_open_receipt(path)

    monkeypatch.setattr(listary_search_module, "open_file_verified", verified)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert opened == [str(tmp_path / "after.mov")]
        assert client.get_item_calls == [result.item_id, result.item_id]
        assert dialog._last_file_manager_receipt is not None
        assert dialog._last_file_manager_receipt.displayed_path == str(tmp_path / "before.mov")
        assert dialog._last_file_manager_receipt.target_path == str(tmp_path / "after.mov")
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_revalidation_rejects_replaced_stable_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "scene.mov"
    result = ListarySearchResult(
        item_id=81,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
        root_id="root-a",
        stable_file_id="stable-before",
        available=True,
    )
    page = ListaryFileSearchPage((result,), 1, 0, 100, False, 3, 1, 100)
    client = _FileSearchClient([page])
    client.items[result.item_id] = ListarySearchResult(
        item_id=result.item_id,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
        root_id=result.root_id,
        stable_file_id="stable-after",
        available=True,
    )
    dialog = ListaryFileSearchDialog(client=client)
    opened: list[str] = []
    monkeypatch.setattr(
        listary_search_module,
        "open_file_verified",
        lambda path, **_kwargs: opened.append(str(path)) or _associated_open_receipt(path),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert opened == []
        assert dialog._last_file_manager_failure_receipt is not None
        assert dialog._last_file_manager_failure_receipt.code == "stale_result"
        assert "was replaced" in dialog._last_file_manager_failure_receipt.detail
        assert client.get_item_calls == [result.item_id]
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_batch_revalidation_is_atomic_before_open(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    first = _file_result(tmp_path, 0, name="first.mov")
    second = _file_result(tmp_path, 1, name="second.mov")
    client = _FileSearchClient([ListaryFileSearchPage((first, second), 2, 0, 100, False, 3, 2, 100)])
    client.items[second.item_id] = None
    dialog = ListaryFileSearchDialog(client=client)
    opened: list[str] = []
    monkeypatch.setattr(
        listary_search_module,
        "open_file_verified",
        lambda path, **_kwargs: opened.append(str(path)) or _associated_open_receipt(path),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1)

        assert dialog._open_current() is False
        assert opened == []
        assert "no longer exists" in dialog.status.text()
        assert client.activations == []
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_multi_select_batches_open_and_copy_paths(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    results = tuple(_file_result(tmp_path, index) for index in range(3))
    client = _FileSearchClient([ListaryFileSearchPage(results, 3, 0, 100, False, 9, 3, 150)])
    dialog = ListaryFileSearchDialog(client=client)
    opened: list[str] = []

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        opened.append(str(path))
        index = len(opened)
        return _associated_open_receipt(path, pid=6200 + index, hwnd=6400 + index)

    monkeypatch.setattr(listary_search_module, "open_file_verified", verified)
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for {path!r}")),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 2)

        assert dialog.open_button.text() == "Open 2"
        assert dialog.copy_path_button.text() == "Copy 2 Paths"
        assert dialog.open_button.isEnabled() is True
        assert dialog.copy_path_button.isEnabled() is True
        assert dialog.reveal_button.isEnabled() is False
        assert "2 items selected" in dialog.details.text()

        assert dialog._copy_current_path() is True
        assert QApplication.clipboard().text() == f"{results[0].path}\n{results[2].path}"
        assert dialog.status.text() == "Copied 2 paths."

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()
        assert opened == [results[0].path, results[2].path]
        assert client.activations == [
            (results[0].item_id, "file_search"),
            (results[2].item_id, "file_search"),
        ]
        assert tuple(receipt.item_id for receipt in dialog._last_file_manager_batch_receipts) == (
            results[0].item_id,
            results[2].item_id,
        )
        assert dialog._last_file_manager_failure_receipt is None
        assert dialog._page_session_valid is False
        assert dialog.status.text().startswith("Verified Open for 2 selected files.")
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_copy_file_name_revalidates_fresh_names_and_rejects_replacement(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    before_a = tmp_path / "before-a.txt"
    before_b = tmp_path / "before-b.txt"
    after_a = tmp_path / "renamed résumé Ω.txt"
    after_b = tmp_path / "moved-two.txt"
    after_a.write_text("a", encoding="utf-8")
    after_b.write_text("b", encoding="utf-8")
    displayed = (
        ListarySearchResult(
            801,
            str(before_a),
            str(tmp_path),
            before_a.name,
            "file",
            root_id="root-copy-name",
            stable_file_id="stable-a",
            available=True,
        ),
        ListarySearchResult(
            802,
            str(before_b),
            str(tmp_path),
            before_b.name,
            "file",
            root_id="root-copy-name",
            stable_file_id="stable-b",
            available=True,
        ),
    )
    client = _FileSearchClient([ListaryFileSearchPage(displayed, 2, 0, 100, False, 19, 2, 80)])
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        client.items[801] = ListarySearchResult(
            801,
            str(after_a),
            str(tmp_path),
            after_a.name,
            "file",
            root_id="root-copy-name",
            stable_file_id="stable-a",
            available=True,
        )
        client.items[802] = ListarySearchResult(
            802,
            str(after_b),
            str(tmp_path),
            after_b.name,
            "file",
            root_id="root-copy-name",
            stable_file_id="stable-b",
            available=True,
        )
        _select_file_search_rows(dialog, 0, 1)

        menu = dialog._create_results_context_menu()
        copy_name = next(
            action for action in menu.actions() if action.objectName() == "spiffyFileSearchContextCopyFileName"
        )
        assert copy_name.isEnabled() is True
        assert dialog._copy_current_file_name() is True
        assert QApplication.clipboard().text() == f"{after_a.name}\n{after_b.name}"
        assert dialog.status.text() == "Copied 2 file names."

        client.items[801] = ListarySearchResult(
            801,
            str(after_a),
            str(tmp_path),
            after_a.name,
            "file",
            root_id="root-copy-name",
            stable_file_id="replacement-a",
            available=True,
        )
        QApplication.clipboard().setText("sentinel")
        assert dialog._copy_current_file_name() is False
        assert QApplication.clipboard().text() == "sentinel"
        assert "changed" in dialog.status.text().casefold() or "refresh" in dialog.status.text().casefold()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_copy_items_revalidates_fresh_paths_and_rejects_replacement(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    before_a = tmp_path / "before-copy-a.txt"
    before_b = tmp_path / "before-copy-b"
    after_a = tmp_path / "moved-copy-a.txt"
    after_b = tmp_path / "moved-copy-b"
    after_a.write_text("a", encoding="utf-8")
    after_b.mkdir()
    displayed = (
        ListarySearchResult(
            811,
            str(before_a),
            str(tmp_path),
            before_a.name,
            "file",
            root_id="root-copy-items",
            stable_file_id="stable-copy-a",
            available=True,
        ),
        ListarySearchResult(
            812,
            str(before_b),
            str(tmp_path),
            before_b.name,
            "folder",
            root_id="root-copy-items",
            stable_file_id="stable-copy-b",
            available=True,
        ),
    )
    client = _FileSearchClient([ListaryFileSearchPage(displayed, 2, 0, 100, False, 19, 2, 80)])
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        client.items[811] = ListarySearchResult(
            811,
            str(after_a),
            str(tmp_path),
            after_a.name,
            "file",
            root_id="root-copy-items",
            stable_file_id="stable-copy-a",
            available=True,
        )
        client.items[812] = ListarySearchResult(
            812,
            str(after_b),
            str(tmp_path),
            after_b.name,
            "folder",
            root_id="root-copy-items",
            stable_file_id="stable-copy-b",
            available=True,
        )
        _select_file_search_rows(dialog, 0, 1)

        menu = dialog._create_results_context_menu()
        copy_items = next(
            action for action in menu.actions() if action.objectName() == "spiffyFileSearchContextCopyItems"
        )
        assert copy_items.isEnabled() is True
        assert dialog._copy_current_items() is True
        mime = QApplication.clipboard().mimeData()
        assert [Path(url.toLocalFile()).resolve(strict=False) for url in mime.urls()] == [
            after_a.resolve(strict=False),
            after_b.resolve(strict=False),
        ]
        assert mime.text() == f"{after_a}\n{after_b}"
        assert dialog.status.text() == "Copied 2 items to the system clipboard."

        client.items[811] = ListarySearchResult(
            811,
            str(after_a),
            str(tmp_path),
            after_a.name,
            "file",
            root_id="root-copy-items",
            stable_file_id="replacement-copy-a",
            available=True,
        )
        QApplication.clipboard().setText("sentinel-copy-items")
        assert dialog._copy_current_items() is False
        assert QApplication.clipboard().text() == "sentinel-copy-items"
        assert "changed" in dialog.status.text().casefold() or "refresh" in dialog.status.text().casefold()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_multi_select_direct_apps_launch_in_selection_order(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    results = tuple(
        ListarySearchResult(
            item_id=920 + index,
            path=str(tmp_path / f"Fixture Batch App {index + 1}.exe"),
            parent_path=str(tmp_path),
            name=f"Fixture Batch App {index + 1}.exe",
            kind="app",
            extension="exe",
            root_id="root-batch-app",
            stable_file_id=f"stable-batch-app-{index + 1}",
            available=True,
        )
        for index in range(3)
    )
    for result in results:
        Path(result.path).write_bytes(b"fixture")
    client = _FileSearchClient([ListaryFileSearchPage(results, 3, 0, 100, False, 15, 3, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    launched: list[str] = []

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        launched.append(str(path))
        index = len(launched)
        return _direct_app_receipt(path, pid=9200 + index, hwnd=9400 + index)

    monkeypatch.setattr(listary_search_module, "open_app_verified", verified)
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for direct app {path!r}")),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 2)

        assert dialog.open_button.isEnabled() is True
        assert "verified executable launches" in dialog.details.text().lower()
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        assert launched == [results[0].path, results[2].path]
        assert client.activations == [
            (results[0].item_id, "file_search"),
            (results[2].item_id, "file_search"),
        ]
        batch = dialog._last_file_manager_batch_receipt
        assert batch is not None
        assert batch.operation == "launch_app_batch"
        assert batch.outcome == "verified"
        assert batch.requested_item_ids == (results[0].item_id, results[2].item_id)
        assert tuple(receipt.operation for receipt in batch.completed) == ("launch_app", "launch_app")
        assert tuple(receipt.item_id for receipt in batch.completed) == (
            results[0].item_id,
            results[2].item_id,
        )
        assert dialog._last_file_manager_failure_receipt is None
        assert dialog._page_session_valid is False
        assert dialog.status.text().startswith("Verified launch for 2 selected apps.")
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_app_batch_revalidation_is_atomic_before_launch(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    results = tuple(
        ListarySearchResult(
            item_id=930 + index,
            path=str(tmp_path / f"Atomic Batch App {index + 1}.exe"),
            parent_path=str(tmp_path),
            name=f"Atomic Batch App {index + 1}.exe",
            kind="app",
            extension="exe",
            root_id="root-atomic-app",
            stable_file_id=f"stable-atomic-app-{index + 1}",
            available=True,
        )
        for index in range(2)
    )
    for result in results:
        Path(result.path).write_bytes(b"fixture")
    client = _FileSearchClient([ListaryFileSearchPage(results, 2, 0, 100, False, 16, 2, 100)])
    client.items[results[1].item_id] = None
    dialog = ListaryFileSearchDialog(client=client)
    launched: list[str] = []
    monkeypatch.setattr(
        listary_search_module,
        "open_app_verified",
        lambda path, **_kwargs: launched.append(str(path)) or _direct_app_receipt(path),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1)

        assert dialog.open_button.isEnabled() is True
        assert dialog._open_current() is False
        assert launched == []
        assert client.activations == []
        assert "no longer exists" in dialog.status.text()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_partial_app_batch_stops_and_records_only_verified_launches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    results = tuple(
        ListarySearchResult(
            item_id=940 + index,
            path=str(tmp_path / f"Partial Batch App {index + 1}.exe"),
            parent_path=str(tmp_path),
            name=f"Partial Batch App {index + 1}.exe",
            kind="app",
            extension="exe",
            root_id="root-partial-app",
            stable_file_id=f"stable-partial-app-{index + 1}",
            available=True,
        )
        for index in range(3)
    )
    for result in results:
        Path(result.path).write_bytes(b"fixture")
    client = _FileSearchClient([ListaryFileSearchPage(results, 3, 0, 100, False, 17, 3, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    attempts: list[str] = []

    def launch_until_second(path, *, revalidate_target, **_kwargs):
        attempts.append(str(path))
        revalidate_target()
        if str(path) == results[1].path:
            destination = _direct_app_receipt(path, pid=10202, hwnd=10404)
            raise AssociatedOpenError(
                "delivery_unverifiable",
                "fixture app accepted the second launch without exact readback",
                ambiguous=True,
                dispatch_happened=True,
                destination_process=destination.destination_process,
                destination_window=destination.destination_window,
            )
        return _direct_app_receipt(path, pid=10201, hwnd=10403)

    monkeypatch.setattr(listary_search_module, "open_app_verified", launch_until_second)
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for direct app {path!r}")),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1, 2)

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        assert attempts == [results[0].path, results[1].path]
        assert client.activations == [(results[0].item_id, "file_search")]
        batch = dialog._last_file_manager_batch_receipt
        assert batch is not None
        assert batch.operation == "launch_app_batch"
        assert batch.outcome == "partial"
        assert tuple(receipt.item_id for receipt in batch.completed) == (results[0].item_id,)
        failure = dialog._last_file_manager_failure_receipt
        assert failure is not None
        assert failure.code == "delivery_unverifiable"
        assert failure.ambiguous is True
        assert failure.dispatch_happened is True
        assert dialog._page_session_valid is False
        assert dialog.status.text().startswith("Verified 1 of 3 selected app launches; stopped")
        assert "no retry" in dialog.status.text().casefold()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_shift_keyboard_extends_row_selection(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    results = tuple(_file_result(tmp_path, index) for index in range(3))
    client = _FileSearchClient([ListaryFileSearchPage(results, 3, 0, 100, False, 14, 3, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        dialog.results.setFocus()
        QTest.keyClick(dialog.results, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
        app.processEvents()

        assert dialog._selected_results() == results[:2]
        assert dialog.open_button.text() == "Open 2"
        assert dialog.copy_path_button.text() == "Copy 2 Paths"
        assert dialog.reveal_button.isEnabled() is False

        dialog.results.clearSelection()
        app.processEvents()
        assert dialog._selected_results() == ()
        assert dialog.open_button.isEnabled() is False
        assert dialog.copy_path_button.isEnabled() is False
        assert dialog.reveal_button.isEnabled() is False
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_batch_validation_fails_closed_before_any_action(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    normal = _file_result(tmp_path, 0)
    malformed_provider = ListarySearchResult(
        item_id=80,
        path=str(tmp_path / "bad.workspaceapp"),
        parent_path=str(tmp_path),
        name="bad.workspaceapp",
        kind="app",
        preview_hint="workspace_application_v1:{not-json",
    )
    client = _FileSearchClient(
        [ListaryFileSearchPage((normal, malformed_provider), 2, 0, 100, False, 10, 2, 120)]
    )
    dialog = ListaryFileSearchDialog(client=client)
    opened: list[str] = []
    monkeypatch.setattr(listary_search_module, "open_path", lambda path: opened.append(str(path)) or True)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1)

        assert dialog.open_button.isEnabled() is False
        assert dialog.copy_path_button.isEnabled() is False
        assert dialog.reveal_button.isEnabled() is False
        assert "Batch actions disabled" in dialog.details.text()
        assert dialog._open_current() is False
        assert opened == []
        assert client.activations == []
        assert "metadata is invalid" in dialog.status.text()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_partial_batch_open_stops_and_records_only_successes(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    results = tuple(_file_result(tmp_path, index) for index in range(3))
    client = _FileSearchClient([ListaryFileSearchPage(results, 3, 0, 100, False, 11, 3, 130)])
    dialog = ListaryFileSearchDialog(client=client)
    attempts: list[str] = []

    def open_until_second(path: str, *, revalidate_target, **_kwargs):
        attempts.append(str(path))
        revalidate_target()
        if str(path) == results[1].path:
            destination = _associated_open_receipt(path, pid=7202, hwnd=7404)
            raise AssociatedOpenError(
                "delivery_unverifiable",
                "fixture handler accepted the second file without exact readback",
                ambiguous=True,
                dispatch_happened=True,
                destination_process=destination.destination_process,
                destination_window=destination.destination_window,
            )
        return _associated_open_receipt(path, pid=7201, hwnd=7403)

    monkeypatch.setattr(listary_search_module, "open_file_verified", open_until_second)
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for {path!r}")),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1, 2)

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()
        assert attempts == [results[0].path, results[1].path]
        assert client.activations == [(results[0].item_id, "file_search")]
        assert tuple(receipt.item_id for receipt in dialog._last_file_manager_batch_receipts) == (
            results[0].item_id,
        )
        failure = dialog._last_file_manager_failure_receipt
        assert failure is not None
        assert failure.code == "delivery_unverifiable"
        assert failure.ambiguous is True
        assert failure.dispatch_happened is True
        assert dialog._page_session_valid is False
        assert dialog.status.text().startswith("Verified 1 of 3 selected file opens; stopped")
        assert "no retry" in dialog.status.text().casefold()
        assert "refresh before continuing paging" in dialog.status.text().casefold()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_batch_cancel_after_verified_open_retains_completed_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    results = tuple(_file_result(tmp_path, index) for index in range(2))
    client = _FileSearchClient([ListaryFileSearchPage(results, 2, 0, 100, False, 13, 2, 130)])
    dialog = ListaryFileSearchDialog(client=client)
    first_verified = threading.Event()
    attempts: list[str] = []

    def verified_until_cancel(path: str, *, cancel_event, revalidate_target, **_kwargs):
        attempts.append(str(path))
        revalidate_target()
        if len(attempts) == 1:
            first_verified.set()
            return _associated_open_receipt(path, pid=8201, hwnd=8403)
        assert cancel_event.wait(2.0) is True
        raise AssociatedOpenError(
            "cancelled",
            "fixture batch cancelled before the second Shell dispatch",
            ambiguous=False,
            dispatch_happened=False,
        )

    monkeypatch.setattr(listary_search_module, "open_file_verified", verified_until_cancel)
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for {path!r}")),
    )
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1)

        assert dialog._open_current() is True
        _wait_until(app, first_verified.is_set)
        dialog._cancel_file_manager_action()
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        assert attempts == [results[0].path, results[1].path]
        assert client.activations == [(results[0].item_id, "file_search")]
        assert tuple(receipt.item_id for receipt in dialog._last_file_manager_batch_receipts) == (
            results[0].item_id,
        )
        batch = dialog._last_file_manager_batch_receipt
        assert batch is not None
        assert batch.outcome == "partial"
        assert batch.failure is not None
        assert batch.failure.code == "cancelled"
        assert batch.failure.ambiguous is False
        assert batch.failure.dispatch_happened is False
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_retired_batch_does_not_invalidate_replacement_query_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    results = tuple(_file_result(tmp_path, index) for index in range(2))
    client = _FileSearchClient([ListaryFileSearchPage(results, 2, 0, 100, False, 15, 2, 130)])
    dialog = ListaryFileSearchDialog(client=client)
    first_verified = threading.Event()
    attempts: list[str] = []

    def verified_until_query_change(path: str, *, cancel_event, revalidate_target, **_kwargs):
        attempts.append(str(path))
        revalidate_target()
        if len(attempts) == 1:
            first_verified.set()
            return _associated_open_receipt(path, pid=9201, hwnd=9403)
        assert cancel_event.wait(2.0) is True
        raise AssociatedOpenError(
            "cancelled",
            "fixture batch retired by replacement query before the second Shell dispatch",
            ambiguous=False,
            dispatch_happened=False,
        )

    monkeypatch.setattr(listary_search_module, "open_file_verified", verified_until_query_change)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1)

        assert dialog._open_current() is True
        _wait_until(app, first_verified.is_set)
        dialog.search.setText("replacement query")
        dialog._debounce.stop()
        replacement_generation = dialog._generation
        assert dialog._page_session_valid is True

        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        assert attempts == [results[0].path, results[1].path]
        assert client.activations == [(results[0].item_id, "file_search")]
        assert dialog._generation == replacement_generation
        assert dialog._page_session_valid is True
        assert dialog._last_file_manager_batch_receipt is None
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_new_query_replaces_multi_selection_with_fresh_page(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    first_results = tuple(_file_result(tmp_path, index) for index in range(3))
    fresh = _file_result(tmp_path, 9, name="fresh.mov")
    client = _FileSearchClient(
        [
            ListaryFileSearchPage(first_results, 3, 0, 100, False, 12, 3, 110),
            ListaryFileSearchPage((fresh,), 1, 0, 100, False, 12, 1, 90),
        ]
    )
    dialog = ListaryFileSearchDialog(client=client)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 2)
        assert len(dialog._selected_results()) == 2

        dialog.search.setText("fresh")
        dialog._debounce.stop()
        dialog._start_search()
        _wait_file_search(app, dialog)

        assert dialog._selected_results() == (fresh,)
        assert dialog.open_button.text() == "Open"
        assert dialog.copy_path_button.text() == "Copy Path"
        assert "items selected" not in dialog.details.text()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_double_click_opens_only_clicked_row_from_multi_selection(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    results = tuple(_file_result(tmp_path, index) for index in range(3))
    client = _FileSearchClient([ListaryFileSearchPage(results, 3, 0, 100, False, 13, 3, 100)])
    dialog = ListaryFileSearchDialog(client=client)
    opened: list[str] = []

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        opened.append(str(path))
        return _associated_open_receipt(path)

    monkeypatch.setattr(listary_search_module, "open_file_verified", verified)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _select_file_search_rows(dialog, 0, 1, 2)

        clicked = dialog.results.item(1, 0)
        assert clicked is not None
        dialog._open_double_clicked(clicked)
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()
        assert opened == [results[1].path]
        assert client.activations == [(results[1].item_id, "file_search")]
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_preview_ignores_stale_broker_callbacks(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    first = _preview_file_result(tmp_path, 1, name="first.mov", preview_hint="video")
    second = _preview_file_result(tmp_path, 2, name="second.mov", preview_hint="video")
    client = _FileSearchClient([ListaryFileSearchPage((first, second), 2, 0, 100, False, 15, 2, 100)])
    broker = _PreviewBroker()
    dialog = ListaryFileSearchDialog(client=client, preview_broker=broker)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        dialog._preview_timer.stop()
        dialog._start_preview()
        _wait_until(app, lambda: bool(broker.requests))
        assert broker.requests[-1][0] == first.path
        first_key = dialog._preview_key

        _select_file_search_rows(dialog, 1)
        dialog._preview_timer.stop()
        dialog._start_preview()
        _wait_until(app, lambda: len(broker.requests) >= 2)
        assert broker.requests[-1][0] == second.path
        second_key = dialog._preview_key
        assert second_key != first_key
        assert broker.cancelled

        stale = QImage(32, 18, QImage.Format.Format_RGB32)
        stale.fill(QColor("#cc0000"))
        broker.imageReady.emit(first_key, stale)
        app.processEvents()
        assert dialog.preview_name.text() == second.name
        assert dialog.preview_status.text() == "Loading preview..."

        current = QImage(64, 36, QImage.Format.Format_RGB32)
        current.fill(QColor("#00cc00"))
        broker.imageReady.emit(second_key, current)
        app.processEvents()
        assert dialog.preview_name.text() == second.name
        assert dialog.preview_status.text() == "Preview ready • 64×36"
        assert dialog.preview_image.pixmap() is not None
        assert dialog.preview_image.pixmap().isNull() is False
    finally:
        dialog.shutdown()
        dialog.deleteLater()


def test_deep_file_search_real_shared_thumbnail_owner_decodes_image_off_ui(tmp_path: Path) -> None:
    from spiffy_ui.media_surface import SharedThumbnailBroker

    app = QApplication.instance() or QApplication([])
    source = tmp_path / "poster.png"
    image = QImage(180, 100, QImage.Format.Format_RGB32)
    image.fill(QColor("#2468a0"))
    assert image.save(str(source)) is True
    result = ListarySearchResult(
        item_id=901,
        path=str(source),
        parent_path=str(tmp_path),
        name=source.name,
        kind="file",
        extension="png",
        size_bytes=source.stat().st_size,
        modified_unix_ms=int(source.stat().st_mtime * 1000),
        preview_hint="image",
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 16, 1, 100)])
    broker = SharedThumbnailBroker(tmp_path / "preview-cache", memory_limit=32, disk_limit_bytes=16 * 1024 * 1024)
    dialog = ListaryFileSearchDialog(client=client, preview_broker=broker)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _wait_until(app, lambda: dialog.preview_status.text().startswith("Preview ready"), timeout=3.0)

        assert dialog.preview_status.text() == "Preview ready • 180×100"
        assert dialog.preview_image.pixmap() is not None
        assert dialog.preview_image.pixmap().isNull() is False
        diagnostics = broker.diagnostics()
        assert diagnostics["decode_requests"] == 1
        assert diagnostics["worker_limit"] == 1
    finally:
        dialog.shutdown()
        assert broker.shutdown(2_000) is True
        dialog.deleteLater()
        broker.deleteLater()
        app.processEvents()


def test_deep_file_search_real_shared_thumbnail_owner_decodes_bmp_off_ui(tmp_path: Path) -> None:
    from spiffy_ui.media_surface import SharedThumbnailBroker

    app = QApplication.instance() or QApplication([])
    source = tmp_path / "poster.bmp"
    image = QImage(96, 64, QImage.Format.Format_RGB32)
    image.fill(QColor("#385a72"))
    assert image.save(str(source)) is True
    result = ListarySearchResult(
        item_id=905,
        path=str(source),
        parent_path=str(tmp_path),
        name=source.name,
        kind="file",
        extension="bmp",
        size_bytes=source.stat().st_size,
        modified_unix_ms=int(source.stat().st_mtime * 1000),
        preview_hint="image",
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 16, 1, 100)])
    broker = SharedThumbnailBroker(tmp_path / "preview-cache", memory_limit=32, disk_cache_enabled=False)
    dialog = ListaryFileSearchDialog(client=client, preview_broker=broker)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _wait_until(app, lambda: dialog.preview_status.text().startswith("Preview ready"), timeout=3.0)

        assert dialog.preview_status.text() == "Preview ready • 96×64"
        assert dialog.preview_image.pixmap() is not None
        assert dialog.preview_image.pixmap().isNull() is False
        assert broker.diagnostics()["decode_requests"] == 1
    finally:
        dialog.shutdown()
        assert broker.shutdown(2_000) is True
        dialog.deleteLater()
        broker.deleteLater()
        app.processEvents()


def test_deep_file_search_unicode_text_preview_is_bounded_and_structure_preserving(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    prefix = "Résumé Ω\n# Heading\n\tIndented line\n"
    content = prefix + ("bounded line αβγ\n" * 5_000)
    result = _preview_file_result(
        tmp_path,
        906,
        name="résumé-notes.txt",
        preview_hint="text",
        content=content.encode("utf-8"),
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 21, 1, 100)])
    dialog = ListaryFileSearchDialog(client=client, preview_broker=_PreviewBroker())
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _wait_until(app, lambda: dialog.preview_status.text().startswith("Text preview ready"), timeout=3.0)

        rendered = dialog.preview_text.toPlainText()
        assert rendered.startswith(prefix)
        assert "Résumé Ω" in rendered
        assert "\tIndented line" in rendered
        assert "preview truncated" in rendered
        assert len(rendered) < 7_000
        assert dialog.preview_text.isHidden() is False
        assert client.get_item_calls.count(result.item_id) >= 2
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_preview_preserves_existing_context_menu_actions(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    result = _preview_file_result(
        tmp_path,
        913,
        name="context.txt",
        preview_hint="text",
        content="Context menu preview Ω".encode("utf-8"),
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 27, 1, 100)])
    dialog = ListaryFileSearchDialog(client=client, preview_broker=_PreviewBroker())
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _wait_until(app, lambda: dialog.preview_status.text().startswith("Text preview ready"), timeout=3.0)

        menu = dialog._create_results_context_menu()
        actions = {action.objectName(): action for action in menu.actions() if action.objectName()}
        assert actions["spiffyFileSearchContextOpen"].isEnabled() is True
        assert actions["spiffyFileSearchContextReveal"].isEnabled() is True
        assert actions["spiffyFileSearchContextCopyPath"].isEnabled() is True
        assert actions["spiffyFileSearchContextCopyFileName"].isEnabled() is True
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_active_preview_is_retired_when_at_filter_is_accepted(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    result = _preview_file_result(
        tmp_path,
        914,
        name="active-preview.txt",
        preview_hint="text",
        content="Active preview Ω".encode("utf-8"),
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 28, 1, 100)])
    dialog = ListaryFileSearchDialog(client=client, preview_broker=_PreviewBroker())
    try:
        dialog.show()
        dialog._start_search()
        _wait_file_search(app, dialog)
        _wait_until(app, lambda: dialog.preview_status.text().startswith("Text preview ready"), timeout=3.0)
        preview_generation = dialog._preview_generation
        assert "Active preview Ω" in dialog.preview_text.toPlainText()

        dialog.search.setText("@p")
        dialog._debounce.stop()
        dialog.search.setCursorPosition(2)
        app.processEvents()
        assert dialog.filter_discovery.isVisible() is True
        assert dialog._accept_filter_discovery() is True
        dialog._debounce.stop()

        assert dialog.search.text() == "ext:pdf"
        assert dialog.filter_chips[2].isChecked() is True
        assert dialog.preview_text.toPlainText() == ""
        assert dialog.preview_text.isHidden() is True
        assert dialog._preview_generation > preview_generation
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_pdf_preview_renders_only_first_page_with_bounded_text_surface(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "preview.pdf"
    writer = QPdfWriter(str(source))
    painter = QPainter(writer)
    painter.drawText(120, 160, "Spiffy PDF first page")
    assert painter.end() is True
    del painter
    del writer
    stat = source.stat()
    result = ListarySearchResult(
        item_id=907,
        path=str(source),
        parent_path=str(tmp_path),
        name=source.name,
        kind="file",
        extension="pdf",
        size_bytes=stat.st_size,
        modified_unix_ms=stat.st_mtime_ns // 1_000_000,
        preview_hint="pdf",
        root_id="preview-root",
        stable_file_id="preview:907",
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 22, 1, 100)])
    dialog = ListaryFileSearchDialog(client=client, preview_broker=_PreviewBroker())
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _wait_until(app, lambda: dialog.preview_status.text().startswith("PDF preview ready"), timeout=3.0)

        assert "first of 1 page" in dialog.preview_status.text()
        assert dialog.preview_image.pixmap() is not None
        assert dialog.preview_image.pixmap().isNull() is False
        assert dialog.preview_text.toPlainText()
        assert "OCR was not started" in dialog.preview_text.toPlainText() or len(dialog.preview_text.toPlainText()) <= 2_100
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_audio_preview_reuses_media_probe_and_fails_closed_for_playback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "short.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * 1_600)
    stat = source.stat()
    result = ListarySearchResult(
        item_id=908,
        path=str(source),
        parent_path=str(tmp_path),
        name=source.name,
        kind="file",
        extension="wav",
        size_bytes=stat.st_size,
        modified_unix_ms=stat.st_mtime_ns // 1_000_000,
        preview_hint="audio",
        root_id="preview-root",
        stable_file_id="preview:908",
    )
    probes: list[str] = []
    monkeypatch.setattr(
        "workflow.production_contracts_common.ffprobe_media",
        lambda path, **_kwargs: probes.append(str(path)) or {
            "status": "PASS",
            "duration_s": 0.1,
            "audio_codec": "pcm_s16le",
            "audio_sample_rate": 16_000,
            "audio_channels": 1,
            "has_audio": True,
        },
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 23, 1, 100)])
    dialog = ListaryFileSearchDialog(client=client, preview_broker=_PreviewBroker())
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _wait_until(app, lambda: dialog.preview_status.text().startswith("Audio metadata ready"), timeout=3.0)

        assert probes == [str(source)]
        preview = dialog.preview_text.toPlainText()
        assert "Duration: 0.10 s" in preview
        assert "Codec: pcm_s16le" in preview
        assert "Playback: unavailable in File Search" in preview
        assert "standalone lease-bound canonical playback owner" in preview
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_document_preview_stale_completion_cannot_overwrite_new_selection(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    first = _preview_file_result(tmp_path, 909, name="first.txt", preview_hint="text", content=b"FIRST PREVIEW")
    second = _preview_file_result(tmp_path, 910, name="second.txt", preview_hint="text", content=b"SECOND PREVIEW")
    page = ListaryFileSearchPage((first, second), 2, 0, 100, False, 24, 2, 100)
    client = _BlockingPreviewFileSearchClient(page, first.item_id)
    dialog = ListaryFileSearchDialog(client=client, preview_broker=_PreviewBroker())
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        dialog._preview_timer.stop()
        dialog._start_preview()
        assert client.preview_started.wait(1.0)

        _select_file_search_rows(dialog, 1)
        dialog._preview_timer.stop()
        dialog._start_preview()
        _wait_until(app, lambda: "SECOND PREVIEW" in dialog.preview_text.toPlainText(), timeout=2.0)

        client.preview_release.set()
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()
        assert dialog.preview_name.text() == second.name
        assert dialog.preview_text.toPlainText() == "SECOND PREVIEW"
    finally:
        client.preview_release.set()
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_close_cancels_inflight_document_preview_without_late_ui_write(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    result = _preview_file_result(tmp_path, 911, name="blocked.txt", preview_hint="text", content=b"LATE PREVIEW")
    page = ListaryFileSearchPage((result,), 1, 0, 100, False, 25, 1, 100)
    client = _BlockingPreviewFileSearchClient(page, result.item_id)
    dialog = ListaryFileSearchDialog(client=client, preview_broker=_PreviewBroker())
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        dialog._preview_timer.stop()
        dialog._start_preview()
        assert client.preview_started.wait(1.0)
        generation = dialog._preview_generation

        dialog.reject()
        assert dialog._preview_generation > generation
        client.preview_release.set()
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()

        assert "LATE PREVIEW" not in dialog.preview_text.toPlainText()
        assert dialog._preview_key == ""
    finally:
        client.preview_release.set()
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_broken_pdf_preview_fails_closed_without_retry(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    result = _preview_file_result(
        tmp_path,
        912,
        name="broken.pdf",
        preview_hint="pdf",
        content=b"not a pdf",
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 26, 1, 100)])
    dialog = ListaryFileSearchDialog(client=client, preview_broker=_PreviewBroker())
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        _wait_until(app, lambda: "valid first page" in dialog.preview_status.text(), timeout=3.0)

        assert client.get_item_calls == [result.item_id]
        assert "valid first page" in dialog.preview_status.text()
        assert dialog.preview_text.toPlainText() == ""
    finally:
        assert dialog.shutdown() is True
        dialog.deleteLater()


def test_deep_file_search_preview_skips_unsupported_and_multi_selection(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    unsupported_result = ListarySearchResult(
        item_id=902,
        path=str(tmp_path / "archive.bin"),
        parent_path=str(tmp_path),
        name="archive.bin",
        kind="file",
        extension="bin",
        preview_hint="",
    )
    video_result = _preview_file_result(tmp_path, 904, name="clip.mov", preview_hint="video")
    client = _FileSearchClient(
        [ListaryFileSearchPage((unsupported_result, video_result), 2, 0, 100, False, 17, 2, 100)]
    )
    broker = _PreviewBroker()
    dialog = ListaryFileSearchDialog(client=client, preview_broker=broker)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        dialog._preview_timer.stop()
        assert broker.requests == []
        assert dialog.preview_status.text() == "Preview unavailable for this item type."

        _select_file_search_rows(dialog, 0, 1)
        dialog._preview_timer.stop()
        assert broker.requests == []
        assert dialog.preview_status.text() == "Preview unavailable for 2 selected items."
    finally:
        dialog.shutdown()
        dialog.deleteLater()


def test_deep_file_search_missing_preview_file_fails_from_background_owner(tmp_path: Path) -> None:
    from spiffy_ui.media_surface import SharedThumbnailBroker

    app = QApplication.instance() or QApplication([])
    source = tmp_path / "vanished.png"
    image = QImage(96, 54, QImage.Format.Format_RGB32)
    image.fill(QColor("#774499"))
    assert image.save(str(source)) is True
    result = ListarySearchResult(
        item_id=904,
        path=str(source),
        parent_path=str(tmp_path),
        name=source.name,
        kind="file",
        extension="png",
        size_bytes=source.stat().st_size,
        modified_unix_ms=int(source.stat().st_mtime * 1000),
        preview_hint="image",
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 19, 1, 100)])
    broker = SharedThumbnailBroker(tmp_path / "unused-cache", disk_cache_enabled=False)
    dialog = ListaryFileSearchDialog(client=client, preview_broker=broker)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        source.unlink()
        _wait_until(app, lambda: dialog.preview_status.text() == "Preview source is no longer available.", timeout=3.0)
        assert broker.diagnostics()["failures"] == 0
        assert (tmp_path / "unused-cache").exists() is False
    finally:
        dialog.shutdown()
        assert broker.shutdown(2_000) is True
        dialog.deleteLater()
        broker.deleteLater()
        app.processEvents()


def test_deep_file_search_shutdown_fences_late_preview_callback(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    result = _preview_file_result(tmp_path, 6, name="late.mov", preview_hint="video")
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 20, 1, 100)])
    broker = _PreviewBroker()
    dialog = ListaryFileSearchDialog(client=client, preview_broker=broker)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        dialog._preview_timer.stop()
        dialog._start_preview()
        _wait_until(app, lambda: bool(broker.requests))
        key = dialog._preview_key
        assert key

        assert dialog.shutdown() is True
        late = QImage(80, 45, QImage.Format.Format_RGB32)
        late.fill(QColor("#55aa33"))
        broker.imageReady.emit(key, late)
        app.processEvents()

        assert dialog._preview_key == ""
        assert not dialog.preview_status.text().startswith("Preview ready")
        assert broker.cancelled
    finally:
        dialog.deleteLater()


def test_standalone_search_surface_reuses_resident_client_ui() -> None:
    app = QApplication.instance() or QApplication([])
    created_app, dialog = create_standalone_dialog("spiffy clipboard")
    try:
        app.processEvents()
        assert created_app is app
        assert dialog.windowTitle() == "Search Files & Apps"
        assert dialog.search.text() == "spiffy clipboard"
        assert dialog.isVisible() is True
        assert isinstance(dialog.client, ListaryAgentClient)
        assert dialog.styleSheet()
    finally:
        dialog.reject()
        app.processEvents()


def test_windows_launcher_targets_the_same_resident_search_owner() -> None:
    launcher = Path(__file__).resolve().parents[1] / "Launch Spiffy Listary.bat"
    text = launcher.read_text(encoding="utf-8")
    assert "ListaryMac\\state" in text
    assert "integration-endpoint-v1.json" in text
    assert "listary-agent.exe" in text
    assert "'spiffy_ui.listary_search'" in text
    assert "'--resident-hotkey'" in text
    assert "'--start-hidden'" in text
    assert "Start-Process" in text
    assert "-WindowStyle Hidden" in text
    assert "ui-endpoint-v1.txt" in text
    assert "NamedPipeClientStream" in text
    assert '>"\\\\.\\pipe\\!SPIFFY_LISTARY_UI_PIPE!" echo' not in text
    assert "SPIFFY_LISTARY_STATE_DIR" in text
    assert "listary-agent.install.json" in text
    assert "ui_hotkey_enabled" in text
    assert "SPIFFY_LISTARY_HOTKEY_ENABLED" in text
    assert "-Raw ^| ConvertFrom-Json" not in text
    assert "-Raw | ConvertFrom-Json" in text
    assert "--setup-if-rootless" in text
    assert 'if "!SPIFFY_LISTARY_QUIT_RESIDENT!"=="1" goto :notify_ui_resident' in text
    assert text.index('set "SPIFFY_LISTARY_AGENT_READY=0"') < text.index("\n:notify_ui_resident\n")


def _start_windows_launcher_pipe(server_name: str, *, action: str, setup: str, token: str = "test-secret"):
    """Run the actual batch sender against a test-owned pipe, never its startup path."""
    import re
    import subprocess

    launcher = Path(__file__).resolve().parents[1] / "Launch Spiffy Listary.bat"
    senders = [line.strip() for line in launcher.read_text(encoding="utf-8").splitlines()
               if "NamedPipeClientStream" in line]
    assert len(senders) == 1, "Revisit launcher transport coverage if its sender changes."
    match = re.fullmatch(r'powershell -NoProfile -Command "(.*)" >nul 2>nul', senders[0])
    assert match is not None
    environment = dict(os.environ, SPIFFY_LISTARY_UI_PIPE=server_name,
                       SPIFFY_LISTARY_UI_TOKEN=token, SPIFFY_LISTARY_UI_ACTION=action,
                       SPIFFY_LISTARY_SETUP_IF_ROOTLESS=setup)
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return subprocess.Popen(
        [str(powershell), "-NoProfile", "-Command", match.group(1)],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _finish_windows_launcher_pipe(app: QApplication, process) -> int:
    _wait_until(app, lambda: process.poll() is not None, timeout=5.0)
    stdout, stderr = process.communicate(timeout=1.0)
    assert not stdout
    assert not stderr, stderr.decode("utf-8", errors="replace")[-1200:]
    app.processEvents()
    app.processEvents()
    return process.returncode


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell named-pipe transport")
@pytest.mark.parametrize(
    ("action", "setup", "include_setup"),
    [("show", "1", True), ("show", "0", False), ("show", "", False),
     ("show", "true", False), ("ping", "1", False), ("quit", "1", False)],
)
def test_windows_launcher_pipe_preserves_explicit_setup_and_legacy_payloads(
    tmp_path: Path, action: str, setup: str, include_setup: bool,
) -> None:
    app = QApplication.instance() or QApplication([])
    name = _resident_server_name(tmp_path / "launcher-wire")
    server = QLocalServer()
    socket = None
    try:
        assert server.listen(name)
        process = _start_windows_launcher_pipe(name, action=action, setup=setup)
        _wait_until(app, server.hasPendingConnections, timeout=5.0)
        socket = server.nextPendingConnection()
        assert socket is not None
        _wait_until(app, socket.canReadLine)
        payload = json.loads(bytes(socket.readAll()).strip().decode("utf-8"))
        expected = {"schema_version": 1, "token": "test-secret", "action": action, "query": ""}
        if include_setup:
            expected["setup_if_rootless"] = True
        assert payload == expected
        assert _finish_windows_launcher_pipe(app, process) == 0
    finally:
        if socket is not None:
            socket.close()
        server.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell named-pipe transport")
@pytest.mark.parametrize(
    ("has_usable_root", "token"),
    [(False, "test-secret"), (True, "test-secret"), (False, "wrong-secret")],
    ids=["rootless", "indexed", "wrong-token"],
)
def test_windows_launcher_pipe_explicit_setup_reaches_existing_resident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_usable_root: bool, token: str,
) -> None:
    app = QApplication.instance() or QApplication([])
    name = _resident_server_name(tmp_path / "launcher-resident")
    server = QLocalServer(app)
    events: list[str] = []
    roots = ({"path": str(tmp_path), "enabled": True, "availability": "available"},) if has_usable_root else ()

    class Resident:
        client = type("Roots", (), {"list_roots": lambda self: roots})()

        def _open_root_management(self) -> None:
            events.append("manage")

    monkeypatch.setattr(listary_search_module, "_show_standalone_dialog",
                        lambda _dialog, query: events.append("show:" + query))
    try:
        assert server.listen(name)
        listary_search_module._bind_resident_server(server, Resident(), "test-secret")
        # Each explicit launch gets one authenticated request, with no extra
        # setup from a repeated frame or a failed authentication.
        for request_count in (1, 2):
            process = _start_windows_launcher_pipe(name, action="show", setup="1", token=token)
            assert _finish_windows_launcher_pipe(app, process) == 0
            per_request = [] if token != "test-secret" else (
                ["show:"] if has_usable_root else ["show:", "manage"]
            )
            assert events == per_request * request_count
    finally:
        server.close()
        app.processEvents()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell named-pipe transport")
def test_windows_launcher_pipe_refuses_disconnected_endpoint_then_reconnects(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    name = _resident_server_name(tmp_path / "launcher-reconnect")
    process = _start_windows_launcher_pipe(name, action="show", setup="1")
    assert _finish_windows_launcher_pipe(app, process) == 1
    server = QLocalServer()
    socket = None
    try:
        assert server.listen(name)
        process = _start_windows_launcher_pipe(name, action="show", setup="1")
        _wait_until(app, server.hasPendingConnections, timeout=5.0)
        socket = server.nextPendingConnection()
        assert socket is not None
        _wait_until(app, socket.canReadLine)
        payload = json.loads(bytes(socket.readAll()).strip().decode("utf-8"))
        assert payload == {"schema_version": 1, "token": "test-secret", "action": "show",
                           "query": "", "setup_if_rootless": True}
        assert _finish_windows_launcher_pipe(app, process) == 0
    finally:
        if socket is not None:
            socket.close()
        server.close()


def test_global_hotkey_falls_through_until_an_enabled_index_root_is_reconciled() -> None:
    class RootClient:
        def __init__(self, roots=None, error: Exception | None = None) -> None:
            self.roots = tuple(roots or ())
            self.error = error

        def list_roots(self):
            if self.error is not None:
                raise self.error
            return self.roots

    assert listary_search_module._global_hotkey_activation_ready(RootClient()) is False
    assert (
        listary_search_module._global_hotkey_activation_ready(
            RootClient(({"path": r"C:\Users\ExampleUser\Documents", "enabled": False},))
        )
        is False
    )
    assert (
        listary_search_module._global_hotkey_activation_ready(
            RootClient(({"path": r"C:\Users\ExampleUser\Documents", "enabled": True},))
        )
        is False
    )
    assert (
        listary_search_module._global_hotkey_activation_ready(
            RootClient(
                (
                    {
                        "path": r"C:\Users\ExampleUser\Documents",
                        "enabled": True,
                        "availability": "available",
                        "last_reconciled_unix_ms": 1,
                    },
                )
            )
        )
        is True
    )
    for unavailable in ("unavailable", "unknown", ""):
        assert listary_search_module._global_hotkey_activation_ready(
            RootClient(({
                "path": r"C:\Users\ExampleUser\Documents",
                "enabled": True,
                "availability": unavailable,
                "last_reconciled_unix_ms": 1,
            },))
        ) is False
    assert listary_search_module._global_hotkey_activation_ready(RootClient((
        {"path": "C:/offline", "enabled": True, "availability": "unavailable", "last_reconciled_unix_ms": 1},
        {"path": "C:/available", "enabled": True, "availability": "available", "last_reconciled_unix_ms": 1},
    ))) is True
    assert (
        listary_search_module._global_hotkey_activation_ready(
            RootClient(error=RuntimeError("agent unavailable"))
        )
        is False
    )
    assert listary_search_module._indexed_root_state(RootClient()) == (0, 0)
    assert listary_search_module._explicit_root_setup_needed(RootClient()) is True
    assert (
        listary_search_module._indexed_root_state(
            RootClient(({"path": r"C:\Users\ExampleUser\Documents", "enabled": False},))
        )
        == (1, 0)
    )
    assert (
        listary_search_module._explicit_root_setup_needed(
            RootClient(({"path": r"C:\Users\ExampleUser\Documents", "enabled": False},))
        )
        is True
    )
    assert (
        listary_search_module._explicit_root_setup_needed(
            RootClient(({"path": r"C:\Users\ExampleUser\Documents", "enabled": True},))
        )
        is False
    )
    assert listary_search_module._explicit_root_setup_needed(
        RootClient(error=RuntimeError("agent unavailable"))
    ) is False


@pytest.mark.parametrize("setup_if_rootless", (False, True))
def test_resident_activation_endpoint_is_profile_scoped_and_forwards_query(
    tmp_path: Path, setup_if_rootless: bool
) -> None:
    app = QApplication.instance() or QApplication([])
    first_name = _resident_server_name(tmp_path / "profile-a")
    second_name = _resident_server_name(tmp_path / "profile-b")
    assert first_name != second_name
    QLocalServer.removeServer(first_name)
    server = QLocalServer()
    try:
        assert server.listen(first_name)
        result: dict[str, bool] = {}
        client_thread = threading.Thread(
            target=lambda: result.setdefault(
                "ok",
                _notify_existing_resident(
                    first_name, "spiffy clipboard", setup_if_rootless=setup_if_rootless, timeout_ms=500
                ),
            ),
            daemon=True,
        )
        client_thread.start()
        connected, timed_out = server.waitForNewConnection(500)
        assert connected is True
        assert timed_out is False
        local_socket = server.nextPendingConnection()
        assert local_socket is not None
        read_deadline = time.monotonic() + 0.5
        while not local_socket.bytesAvailable() and time.monotonic() < read_deadline:
            app.processEvents()
            time.sleep(0.005)
        assert local_socket.bytesAvailable() > 0
        payload = json.loads(bytes(local_socket.readAll()).strip().decode("utf-8"))
        expected = {"schema_version": 1, "token": "", "action": "show", "query": "spiffy clipboard"}
        if setup_if_rootless:
            expected["setup_if_rootless"] = True
        assert payload == expected
        client_thread.join(timeout=0.5)
        assert result.get("ok") is True
        local_socket.disconnectFromServer()
        app.processEvents()
    finally:
        server.close()
        QLocalServer.removeServer(first_name)


def test_resident_command_rejects_unknown_actions() -> None:
    try:
        _send_resident_command("unused", "unsupported")
    except ValueError as exc:
        assert "unsupported resident action" in str(exc)
    else:
        raise AssertionError("unknown resident action was accepted")


@pytest.mark.parametrize("has_usable_root", (False, True))
def test_existing_resident_explicit_rootless_setup_is_forwarded_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_usable_root: bool
) -> None:
    """An existing tray resident must honor an explicit setup launch, not just show search."""
    app = QApplication.instance() or QApplication([])
    name = _resident_server_name(tmp_path / ("indexed" if has_usable_root else "rootless"))
    QLocalServer.removeServer(name)
    server = QLocalServer(app)
    events: list[str] = []
    roots = ({"path": str(tmp_path), "enabled": True, "availability": "available"},) if has_usable_root else ()

    class Resident:
        client = type("Roots", (), {"list_roots": lambda self: roots})()

        def _open_root_management(self) -> None:
            events.append("manage")

    monkeypatch.setattr(
        listary_search_module,
        "_show_standalone_dialog",
        lambda _dialog, query: events.append("show:" + query),
    )
    try:
        assert server.listen(name)
        listary_search_module._bind_resident_server(server, Resident(), "test-secret")
        result: dict[str, bool] = {}
        sender = threading.Thread(
            target=lambda: result.setdefault(
                "ok",
                _notify_existing_resident(
                    name, "find this file", token="test-secret", setup_if_rootless=True, timeout_ms=500
                ),
            ),
            daemon=True,
        )
        sender.start()
        _wait_until(app, lambda: bool(events), timeout=1.5)
        sender.join(timeout=0.5)
        app.processEvents()
        assert result == {"ok": True}
        assert events == (["show:find this file"] if has_usable_root else ["show:find this file", "manage"])
    finally:
        server.close()
        QLocalServer.removeServer(name)


class _ActionClient:
    def __init__(self) -> None:
        self.activations: list[int] = []

    def record_activation(self, item_id: int, *, source: str = "launcher") -> None:
        self.activations.append(int(item_id))


class _BlockingActivationClient:
    def __init__(self) -> None:
        self.activations: list[tuple[int, str]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.closed = False
        self.closed_before_finished = False

    def record_activation(self, item_id: int, *, source: str = "launcher") -> None:
        self.started.set()
        self.release.wait(2.0)
        self.activations.append((int(item_id), str(source)))
        self.finished.set()

    def close(self) -> None:
        self.closed_before_finished = not self.finished.is_set()
        self.closed = True


class _QuickSearchClient:
    def __init__(self) -> None:
        self.cancelled: list[int] = []
        self.activations: list[int] = []
        self.searches: list[str] = []
        self.search_limits: list[int] = []
        self.pins: set[int] = set()

    def cancel(self, request_id: int) -> None:
        self.cancelled.append(int(request_id))

    def record_activation(self, item_id: int) -> None:
        self.activations.append(int(item_id))

    def toggle_pin(self, item_id: int) -> bool:
        item_id = int(item_id)
        if item_id in self.pins:
            self.pins.remove(item_id)
            return False
        self.pins.add(item_id)
        return True

    def search(
        self,
        query: str,
        *,
        kinds: tuple[str, ...] = (),
        limit: int = 40,
        candidate_limit: int = 4096,
        request_id: int | None = None,
    ) -> ListarySearchPage:
        del kinds, candidate_limit, request_id
        self.searches.append(str(query))
        self.search_limits.append(int(limit))
        return ListarySearchPage(())


def test_quick_search_appearance_menu_uses_shared_owner_and_preserves_search_focus(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    settings = tmp_path / "appearance.json"
    ThemeManager.load(settings, force=True)
    dialog = ListarySearchDialog(client=_QuickSearchClient())
    try:
        dialog.show()
        dialog.search.setFocus()
        app.processEvents()
        assert QApplication.focusWidget() is dialog.search
        assert set(dialog.appearance_actions) == {"broadcast_dark", "listary_slate"}
        assert dialog.appearance_actions["broadcast_dark"].isChecked() is True

        dialog.appearance_actions["listary_slate"].trigger()
        app.processEvents()

        assert shared_appearance_profile_id() == "listary_slate"
        assert dialog.property("spiffyAppearanceProfile") == "listary_slate"
        assert dialog.palette().color(dialog.palette().ColorRole.Window).name() == "#1e242d"
        assert dialog.appearance_actions["listary_slate"].isChecked() is True
        assert dialog.appearance_actions["broadcast_dark"].isChecked() is False
        assert QApplication.focusWidget() is dialog.search
        assert "Listary Slate" in dialog.status.text()
        assert json.loads(settings.read_text(encoding="utf-8"))["profile"] == "listary_slate"
    finally:
        set_shared_appearance_profile("broadcast_dark", settings_path=settings, persist=False)
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_keeps_reference_component_styling_over_shared_appearance(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    settings = tmp_path / "appearance.json"
    ThemeManager.load(settings, force=True)
    dialog = ListaryFileSearchDialog(client=_FileSearchClient([]), preview_broker=None)
    try:
        dialog.show()
        app.processEvents()
        set_shared_appearance_profile("listary_slate", settings_path=settings)
        app.processEvents()
        slate_style = dialog.styleSheet()

        assert dialog.property("spiffyAppearanceProfile") == "listary_slate"
        assert "background: #1e242d" in slate_style
        assert "background: #1a2029" in slate_style
        assert "border-bottom: 2px solid #5f8fd6" in slate_style

        set_shared_appearance_profile("broadcast_dark", settings_path=settings, persist=False)
        app.processEvents()
        broadcast_style = dialog.styleSheet()
        assert "background: #1e242d" in broadcast_style
        assert "border-bottom: 2px solid #4b9cd3" in broadcast_style
    finally:
        set_shared_appearance_profile("broadcast_dark", settings_path=settings, persist=False)
        dialog.reject()
        dialog.deleteLater()


class _RevalidatingQuickSearchClient(_QuickSearchClient):
    def __init__(self, current: ListarySearchResult | None) -> None:
        super().__init__()
        self.current = current
        self.get_item_calls: list[int] = []
        self.activation_sources: list[tuple[int, str]] = []

    def get_item(self, item_id: int) -> ListarySearchResult | None:
        self.get_item_calls.append(int(item_id))
        return self.current

    def record_activation(self, item_id: int, *, source: str = "launcher") -> None:
        self.activation_sources.append((int(item_id), str(source)))


def _select_dialog_result(dialog: ListarySearchDialog, result: ListarySearchResult) -> None:
    row = QListWidgetItem(result.name)
    row.setData(dialog.RESULT_ROLE, result)
    dialog.results.addItem(row)
    dialog.results.setCurrentItem(row)


def test_quick_search_edit_retires_active_result_before_debounce(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "alpha_hold.txt"
    target.write_text("alpha", encoding="utf-8")
    result = ListarySearchResult(
        item_id=501,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
    )
    page = ListarySearchPage((result,), 900, 1)
    client = _QuickSearchClient()
    dialog = ListarySearchDialog(client=client)
    opened: list[str] = []
    revealed: list[str] = []
    monkeypatch.setattr(listary_search_module, "open_path", lambda path: opened.append(str(path)) or True)
    monkeypatch.setattr(listary_search_module, "reveal_in_file_manager", lambda path: revealed.append(str(path)) or True)
    try:
        seeded_generation = dialog._search_generation
        dialog._search_finished(None, seeded_generation, page, None)
        app.processEvents()
        assert dialog.results.count() == 1
        assert dialog.open_button.isEnabled() is True

        dialog._active_request_id = 41
        dialog.search.setText("beta_fresh")
        retired_generation = dialog._search_generation

        assert retired_generation == seeded_generation + 1
        assert dialog.results.count() == 0
        assert dialog.details.text() == "Select a result to see indexed details."
        assert dialog.open_button.isEnabled() is False
        assert dialog.reveal_button.isEnabled() is False
        assert dialog.copy_path_button.isEnabled() is False
        assert dialog.status.text() == "Searching..."
        assert dialog._debounce.isActive() is True

        _wait_until(app, lambda: client.cancelled == [41])
        assert client.cancelled == [41]

        dialog._search_finished(None, seeded_generation, page, None)
        app.processEvents()
        assert dialog.results.count() == 0
        assert dialog.open_button.isEnabled() is False
        assert dialog._open_current() is False
        assert dialog._reveal_current() is False
        assert dialog._copy_current_path() is False
        assert opened == []
        assert revealed == []
        assert client.activations == []
    finally:
        dialog._debounce.stop()
        dialog.reject()
        dialog.deleteLater()


def test_quick_search_rapid_edits_reject_and_same_query_reopen_keep_old_results_retired(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "stale.txt"
    target.write_text("stale", encoding="utf-8")
    result = ListarySearchResult(
        item_id=502,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
    )
    page = ListarySearchPage((result,), 800, 1)
    client = _QuickSearchClient()
    dialog = ListarySearchDialog(client=client)
    try:
        dialog._active_request_id = 77
        start_generation = dialog._search_generation
        dialog.search.setText("a")
        first_generation = dialog._search_generation
        dialog.search.setText("ab")
        second_generation = dialog._search_generation
        dialog.search.setText("abc")
        third_generation = dialog._search_generation

        assert (first_generation, second_generation, third_generation) == (
            start_generation + 1,
            start_generation + 2,
            start_generation + 3,
        )
        assert dialog.results.count() == 0
        assert dialog._debounce.isActive() is True
        _wait_until(app, lambda: client.cancelled == [77])
        assert client.cancelled == [77]

        pre_reject_generation = dialog._search_generation
        dialog.reject()
        assert dialog._search_generation == pre_reject_generation + 1
        assert dialog._debounce.isActive() is False

        dialog._search_finished(None, pre_reject_generation, page, None)
        app.processEvents()
        assert dialog.results.count() == 0
        assert dialog.open_button.isEnabled() is False

        reopen_generation = dialog._search_generation
        dialog.open_for_query("abc")
        assert dialog._search_generation == reopen_generation + 2
        assert dialog._debounce.isActive() is False
        assert dialog.results.count() == 0
        assert dialog.open_button.isEnabled() is False
        _wait_until(app, lambda: client.searches == ["abc"])
        assert client.searches == ["abc"]
    finally:
        dialog._debounce.stop()
        dialog.reject()
        dialog.deleteLater()


def test_quick_search_uses_low_latency_typing_debounce() -> None:
    app = QApplication.instance() or QApplication([])
    dialog = ListarySearchDialog(client=_QuickSearchClient())
    try:
        assert dialog._debounce.interval() == 60
        dialog.search.setText("a")
        dialog.search.setText("ab")
        dialog.search.setText("abc")
        assert dialog._debounce.isActive() is True
        assert dialog._active_request_id is None
    finally:
        dialog._debounce.stop()
        dialog.reject()
        dialog.deleteLater()


def test_open_for_query_dispatches_complete_query_once_without_debounce() -> None:
    app = QApplication.instance() or QApplication([])
    client = _QuickSearchClient()
    dialog = ListarySearchDialog(client=client)
    try:
        dialog.open_for_query("complete query")
        assert dialog._debounce.isActive() is False
        _wait_until(app, lambda: client.searches == ["complete query"])
        assert client.searches == ["complete query"]

        client.searches.clear()
        dialog.reject()
        dialog.open_for_query("")
        assert dialog._debounce.isActive() is False
        _wait_until(app, lambda: client.searches == [""])
        assert client.searches == [""]
    finally:
        dialog._debounce.stop()
        dialog.reject()
        dialog.deleteLater()


def test_quick_search_pin_action_runs_off_ui_thread_and_toggles_selected_result(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "pinned_scene.mov"
    target.write_bytes(b"pin fixture")
    result = ListarySearchResult(
        item_id=777,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
    )
    client = _QuickSearchClient()
    dialog = ListarySearchDialog(client=client)
    try:
        dialog.search.setText("pinned_scene")
        dialog._debounce.stop()
        _select_dialog_result(dialog, result)
        app.processEvents()
        assert dialog.pin_button.isEnabled() is True

        assert dialog._toggle_current_pin() is True
        assert dialog.pin_button.isEnabled() is False
        _wait_until(app, lambda: not dialog._pin_busy)
        assert client.pins == {777}
        assert dialog.status.text() == "Pinned pinned_scene.mov."
        assert dialog.pin_button.isEnabled() is True

        assert dialog._toggle_current_pin() is True
        _wait_until(app, lambda: not dialog._pin_busy)
        assert client.pins == set()
        assert dialog.status.text() == "Unpinned pinned_scene.mov."
        assert dialog.pin_button.isEnabled() is True
    finally:
        dialog._debounce.stop()
        dialog.shutdown()
        dialog.deleteLater()


def test_quick_search_edit_does_not_retire_active_folder_index_generation() -> None:
    app = QApplication.instance() or QApplication([])
    client = _QuickSearchClient()
    dialog = ListarySearchDialog(client=client)
    try:
        dialog._generation = 9
        dialog._active_root_request_id = 88
        dialog.index_button.setEnabled(False)
        dialog._sync_launcher_footer_actions()
        assert dialog.options_index_action.isEnabled() is False
        dialog.search.setText("new query")

        assert dialog._generation == 9
        dialog._index_finished(None, 9, {"report": {"discovered": 3, "errors": 0}}, None)
        app.processEvents()

        assert dialog._active_root_request_id is None
        assert dialog.index_button.isEnabled() is True
        assert dialog.options_index_action.isEnabled() is True
        assert dialog._generation == 9
        assert dialog.status.text() == "Searching..."
        assert dialog._debounce.isActive() is True
    finally:
        dialog._debounce.stop()
        dialog.reject()
        dialog.deleteLater()


def test_result_actions_reveal_and_copy_path_reuse_shared_platform_helpers(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    result = ListarySearchResult(
        item_id=77,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
    )
    dialog = ListarySearchDialog(client=_ActionClient())
    revealed: list[tuple[str, str, str]] = []

    def verified_reveal(path, *, target_kind, revalidate_target, operation, **_kwargs):
        revalidate_target()
        revealed.append((str(path), str(target_kind), str(operation)))
        destination = ExplorerWindowIdentity(
            hwnd=101,
            process_id=202,
            process_start_time_ns=303,
            process_path=r"C:\Windows\explorer.exe",
            process_username=r"DESKTOP\fixture",
            window_class="CabinetWClass",
            location_path=str(tmp_path),
        )
        return FileManagerActionReceipt(
            schema="spiffy_windows_file_manager_action_v1",
            outcome="verified",
            operation=str(operation),
            target_path=str(path),
            target_kind=str(target_kind),
            destination=destination,
            window_reused=True,
            location_path_after=str(tmp_path),
            selected_paths_after=(str(path),),
            destination_ready_latency_ms=1.5,
            action_latency_ms=2.5,
            foreground_after=True,
        )

    monkeypatch.setattr(
        listary_search_module,
        "reveal_in_file_manager_verified",
        verified_reveal,
    )
    try:
        _select_dialog_result(dialog, result)
        app.processEvents()
        assert dialog.open_button.isEnabled() is True
        assert dialog.reveal_button.isEnabled() is True
        assert dialog.copy_path_button.isEnabled() is True

        assert dialog._reveal_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert revealed == [(str(target), "file", "reveal")]
        assert dialog._last_file_manager_receipt is not None
        assert dialog._last_file_manager_receipt.item_id == result.item_id
        assert dialog._last_file_manager_receipt.target_path == str(target)
        assert "Verified reveal in Explorer" in dialog.status.text()

        assert dialog._copy_current_path() is True
        assert QApplication.clipboard().text() == str(target)
        assert dialog.status.text() == f"Copied path for {target.name}."
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_reveal_revalidates_moved_identity_and_uses_fresh_path(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    before = tmp_path / "before Ω.txt"
    after = tmp_path / "after résumé Ω.txt"
    after.write_text("fixture", encoding="utf-8")
    displayed = ListarySearchResult(
        item_id=701,
        path=str(before),
        parent_path=str(tmp_path),
        name=before.name,
        kind="file",
        root_id="root-a",
        stable_file_id="stable-a",
        available=True,
    )
    current = ListarySearchResult(
        item_id=displayed.item_id,
        path=str(after),
        parent_path=str(tmp_path),
        name=after.name,
        kind="file",
        root_id=displayed.root_id,
        stable_file_id=displayed.stable_file_id,
        available=True,
    )
    client = _RevalidatingQuickSearchClient(current)
    dialog = ListarySearchDialog(client=client)
    calls: list[str] = []

    def verified(path, *, revalidate_target, operation, target_kind, **_kwargs):
        revalidate_target()
        calls.append(str(path))
        destination = ExplorerWindowIdentity(
            101, 202, 303, r"C:\Windows\explorer.exe", r"DESKTOP\fixture", "CabinetWClass", str(tmp_path)
        )
        return FileManagerActionReceipt(
            "spiffy_windows_file_manager_action_v1",
            "verified",
            operation,
            str(path),
            target_kind,
            destination,
            True,
            str(tmp_path),
            (str(path),),
            1.0,
            2.0,
            True,
        )

    monkeypatch.setattr(listary_search_module, "reveal_in_file_manager_verified", verified)
    try:
        _select_dialog_result(dialog, displayed)
        app.processEvents()
        assert dialog._reveal_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)

        assert calls == [str(after)]
        assert client.get_item_calls == [displayed.item_id, displayed.item_id]
        assert dialog._last_file_manager_receipt is not None
        assert dialog._last_file_manager_receipt.displayed_path == str(before)
        assert dialog._last_file_manager_receipt.target_path == str(after)
        assert dialog._last_file_manager_failure_receipt is None
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_reveal_replaced_identity_fails_before_shell_dispatch_with_receipt(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    displayed = ListarySearchResult(702, str(target), str(tmp_path), target.name, "file", root_id="root-a", stable_file_id="before")
    current = ListarySearchResult(702, str(target), str(tmp_path), target.name, "file", root_id="root-a", stable_file_id="after")
    client = _RevalidatingQuickSearchClient(current)
    dialog = ListarySearchDialog(client=client)
    monkeypatch.setattr(
        listary_search_module,
        "reveal_in_file_manager_verified",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Shell dispatch must not run")),
    )
    try:
        _select_dialog_result(dialog, displayed)
        app.processEvents()
        assert dialog._reveal_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)

        receipt = dialog._last_file_manager_failure_receipt
        assert receipt is not None
        assert receipt.code == "stale_result"
        assert receipt.ambiguous is False
        assert receipt.dispatch_happened is False
        assert client.get_item_calls == [displayed.item_id]
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_folder_open_records_activation_only_after_verified_explorer_receipt(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    folder = tmp_path / "Folder Ω with spaces"
    folder.mkdir()
    displayed = ListarySearchResult(703, str(folder), str(tmp_path), folder.name, "folder", root_id="root-a", stable_file_id="stable-folder")
    client = _RevalidatingQuickSearchClient(displayed)
    dialog = ListarySearchDialog(client=client)

    def verified(path, *, revalidate_target, operation, target_kind, **_kwargs):
        revalidate_target()
        destination = ExplorerWindowIdentity(
            111, 222, 333, r"C:\Windows\explorer.exe", r"DESKTOP\fixture", "CabinetWClass", str(folder)
        )
        return FileManagerActionReceipt(
            "spiffy_windows_file_manager_action_v1",
            "verified",
            operation,
            str(path),
            target_kind,
            destination,
            False,
            str(folder),
            (),
            4.0,
            7.0,
            True,
        )

    monkeypatch.setattr(listary_search_module, "reveal_in_file_manager_verified", verified)
    _select_dialog_result(dialog, displayed)
    app.processEvents()
    assert dialog._open_current() is True
    _wait_until(app, lambda: not dialog._file_manager_action_workers)
    assert QThreadPool.globalInstance().waitForDone(2_000) is True
    app.processEvents()
    assert dialog._last_file_manager_receipt is not None
    assert dialog._last_file_manager_receipt.operation == "open_folder"
    assert client.activation_sources == [(displayed.item_id, "launcher")]
    dialog.deleteLater()


def test_quick_reveal_ambiguous_shell_failure_is_retained_without_retry(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    displayed = ListarySearchResult(704, str(target), str(tmp_path), target.name, "file", root_id="root-a", stable_file_id="stable-a")
    client = _RevalidatingQuickSearchClient(displayed)
    dialog = ListarySearchDialog(client=client)
    calls: list[int] = []
    destination = ExplorerWindowIdentity(
        121, 232, 343, r"C:\Windows\explorer.exe", r"DESKTOP\fixture", "CabinetWClass", str(tmp_path)
    )

    def fail(*_args, **_kwargs):
        calls.append(1)
        raise FileManagerActionError(
            "selection_timeout",
            "fixture selection may have occurred",
            ambiguous=True,
            destination=destination,
            dispatch_happened=True,
        )

    monkeypatch.setattr(listary_search_module, "reveal_in_file_manager_verified", fail)
    try:
        _select_dialog_result(dialog, displayed)
        app.processEvents()
        assert dialog._reveal_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)

        receipt = dialog._last_file_manager_failure_receipt
        assert calls == [1]
        assert receipt is not None
        assert receipt.code == "selection_timeout"
        assert receipt.ambiguous is True
        assert receipt.dispatch_happened is True
        assert receipt.destination is not None and receipt.destination["hwnd"] == destination.hwnd
        assert "no retry" in dialog.status.text().lower()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_selected_result_details_use_indexed_metadata_without_filesystem_probe(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    result = ListarySearchResult(
        item_id=78,
        path=str(tmp_path / "offline.mov"),
        parent_path=str(tmp_path),
        name="offline.mov",
        kind="file",
        extension="mov",
        size_bytes=1536,
        available=False,
        priority="high",
        preview_hint="video",
    )
    dialog = ListarySearchDialog(client=_ActionClient())
    try:
        _select_dialog_result(dialog, result)
        app.processEvents()
        assert dialog.details.text() == "File  •  1.5 KB  •  Unavailable  •  High priority  •  Video"
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_result_icons_are_selected_from_core_kind_and_preview_hint() -> None:
    base = dict(item_id=1, path="C:/fixture/item", parent_path="C:/fixture", name="item")
    assert _result_icon_name(ListarySearchResult(**base, kind="folder")) == "spiffy-item-folder"
    assert _result_icon_name(ListarySearchResult(**base, kind="app")) == "grid"
    assert _result_icon_name(ListarySearchResult(**base, kind="package")) == "grid"
    assert _result_icon_name(ListarySearchResult(**base, kind="file", preview_hint="image")) == "spiffy-item-image"
    assert _result_icon_name(ListarySearchResult(**base, kind="file", preview_hint="video")) == "spiffy-item-video"
    assert _result_icon_name(ListarySearchResult(**base, kind="file", preview_hint="audio")) == "spiffy-item-audio"
    assert _result_icon_name(ListarySearchResult(**base, kind="file", preview_hint="document")) == "spiffy-item-project"


def test_result_open_uses_shared_platform_owner_and_records_activation(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "SpiffyTool.exe"
    target.write_bytes(b"fixture")
    result = ListarySearchResult(
        item_id=91,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="app",
    )
    client = _ActionClient()
    dialog = ListarySearchDialog(client=client)
    opened: list[str] = []

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        opened.append(str(path))
        return _direct_app_receipt(path)

    monkeypatch.setattr(listary_search_module, "open_app_verified", verified)
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for direct app {path!r}")),
    )
    try:
        _select_dialog_result(dialog, result)
        app.processEvents()
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert opened == [str(target)]
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert client.activations == [91]
        assert dialog._last_file_manager_receipt is not None
        assert dialog._last_file_manager_receipt.operation == "launch_app"
    finally:
        dialog.deleteLater()


def test_quick_open_revalidation_uses_fresh_path_for_same_identity_move(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    before = tmp_path / "before.mov"
    after = tmp_path / "after.mov"
    displayed = ListarySearchResult(
        item_id=191,
        path=str(before),
        parent_path=str(tmp_path),
        name=before.name,
        kind="file",
        root_id="root-a",
        stable_file_id="stable-a",
        available=True,
    )
    current = ListarySearchResult(
        item_id=displayed.item_id,
        path=str(after),
        parent_path=str(tmp_path),
        name=after.name,
        kind=displayed.kind,
        root_id=displayed.root_id,
        stable_file_id=displayed.stable_file_id,
        available=True,
    )
    client = _RevalidatingQuickSearchClient(current)
    dialog = ListarySearchDialog(client=client)
    opened: list[str] = []

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        opened.append(str(path))
        return _associated_open_receipt(path)

    monkeypatch.setattr(listary_search_module, "open_file_verified", verified)
    try:
        _select_dialog_result(dialog, displayed)
        app.processEvents()

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert opened == [str(after)]
        assert client.get_item_calls == [displayed.item_id, displayed.item_id]
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert client.activation_sources == [(displayed.item_id, "launcher")]
        assert dialog._last_file_manager_receipt is not None
        assert dialog._last_file_manager_receipt.operation == "open_file"
        assert dialog._last_file_manager_receipt.displayed_path == str(before)
        assert dialog._last_file_manager_receipt.target_path == str(after)
    finally:
        dialog.deleteLater()


def test_quick_direct_executable_app_uses_verified_launch_receipt(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "Fixture App.exe"
    target.write_bytes(b"fixture")
    result = ListarySearchResult(
        item_id=291,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="app",
        root_id="root-app",
        stable_file_id="stable-app",
        available=True,
    )
    client = _RevalidatingQuickSearchClient(result)
    dialog = ListarySearchDialog(client=client)
    launched: list[str] = []

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        launched.append(str(path))
        return _direct_app_receipt(path)

    monkeypatch.setattr(listary_search_module, "open_app_verified", verified)
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for direct app {path!r}")),
    )
    try:
        _select_dialog_result(dialog, result)
        app.processEvents()

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert launched == [str(target)]
        assert client.get_item_calls == [result.item_id, result.item_id]
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert client.activation_sources == [(result.item_id, "launcher")]
        receipt = dialog._last_file_manager_receipt
        assert receipt is not None
        assert receipt.operation == "launch_app"
        assert receipt.target_path == str(target)
        assert isinstance(receipt.destination, DirectAppLaunchReceipt)
    finally:
        dialog.deleteLater()


def test_quick_direct_executable_shortcut_uses_verified_launch_receipt(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    executable = tmp_path / "Fixture Quick Shortcut Target.exe"
    executable.write_bytes(b"fixture")
    shortcut = tmp_path / "Fixture Quick Shortcut.lnk"
    shortcut.write_bytes(b"fixture shortcut")
    result = ListarySearchResult(
        item_id=293,
        path=str(shortcut),
        parent_path=str(tmp_path),
        name=shortcut.name,
        kind="file",
        extension="lnk",
        root_id="root-shortcut",
        stable_file_id="stable-shortcut",
        available=True,
    )
    client = _RevalidatingQuickSearchClient(result)
    dialog = ListarySearchDialog(client=client)
    launched: list[str] = []
    shortcut_arguments = '--profile "Daily Workspace"'
    monkeypatch.setattr(
        listary_search_module,
        "inspect_shortcut_target",
        lambda path: ShortcutTargetIdentity(str(executable), shortcut_arguments, str(tmp_path)),
    )

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        launched.append(str(path))
        return _direct_shortcut_receipt(path, executable, arguments=shortcut_arguments)

    monkeypatch.setattr(listary_search_module, "open_shortcut_verified", verified)
    monkeypatch.setattr(
        listary_search_module,
        "open_file_verified",
        lambda path, **_kwargs: (_ for _ in ()).throw(AssertionError(f"document Open used for shortcut {path!r}")),
    )
    monkeypatch.setattr(
        listary_search_module,
        "open_path",
        lambda path: (_ for _ in ()).throw(AssertionError(f"generic Open used for verifiable shortcut {path!r}")),
    )
    try:
        _select_dialog_result(dialog, result)
        app.processEvents()
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert launched == [str(shortcut)]
        assert client.get_item_calls == [result.item_id, result.item_id]
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert client.activation_sources == [(result.item_id, "launcher")]
        receipt = dialog._last_file_manager_receipt
        assert receipt is not None
        assert receipt.operation == "launch_shortcut"
        assert receipt.target_path == str(shortcut)
        assert isinstance(receipt.destination, DirectShortcutLaunchReceipt)
        assert receipt.destination.executable_path == str(executable)
        assert receipt.destination.shortcut_arguments == shortcut_arguments
    finally:
        dialog.deleteLater()


def test_quick_direct_executable_app_rejects_replaced_stable_identity_before_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "Fixture Replaced App.exe"
    target.write_bytes(b"fixture")
    displayed = ListarySearchResult(
        item_id=292,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="app",
        root_id="root-app",
        stable_file_id="stable-before",
        available=True,
    )
    current = ListarySearchResult(
        item_id=displayed.item_id,
        path=displayed.path,
        parent_path=displayed.parent_path,
        name=displayed.name,
        kind=displayed.kind,
        root_id=displayed.root_id,
        stable_file_id="stable-after",
        available=True,
    )
    client = _RevalidatingQuickSearchClient(current)
    dialog = ListarySearchDialog(client=client)
    launches: list[str] = []

    monkeypatch.setattr(
        listary_search_module,
        "open_app_verified",
        lambda path, **_kwargs: launches.append(str(path)) or _direct_app_receipt(path),
    )
    try:
        _select_dialog_result(dialog, displayed)
        app.processEvents()

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert launches == []
        assert client.activation_sources == []
        failure = dialog._last_file_manager_failure_receipt
        assert failure is not None
        assert failure.code == "stale_result"
        assert failure.dispatch_happened is False
        assert failure.ambiguous is False
        assert dialog._last_file_manager_receipt is None
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_open_revalidation_rejects_replaced_stable_identity(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "scene.mov"
    displayed = ListarySearchResult(
        item_id=192,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
        root_id="root-a",
        stable_file_id="stable-before",
        available=True,
    )
    current = ListarySearchResult(
        item_id=displayed.item_id,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind=displayed.kind,
        root_id=displayed.root_id,
        stable_file_id="stable-after",
        available=True,
    )
    client = _RevalidatingQuickSearchClient(current)
    dialog = ListarySearchDialog(client=client)
    opened: list[str] = []
    monkeypatch.setattr(
        listary_search_module,
        "open_file_verified",
        lambda path, **_kwargs: opened.append(str(path)) or _associated_open_receipt(path),
    )
    try:
        _select_dialog_result(dialog, displayed)
        app.processEvents()

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert opened == []
        assert client.get_item_calls == [displayed.item_id]
        assert dialog._last_file_manager_failure_receipt is not None
        assert dialog._last_file_manager_failure_receipt.code == "stale_result"
        assert "was replaced" in dialog._last_file_manager_failure_receipt.detail
        assert client.activation_sources == []
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_file_open_ambiguous_delivery_failure_is_retained_without_retry(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "scene Ω.txt"
    target.write_text("fixture", encoding="utf-8")
    displayed = ListarySearchResult(
        194,
        str(target),
        str(tmp_path),
        target.name,
        "file",
        root_id="root-a",
        stable_file_id="stable-open",
        available=True,
    )
    client = _RevalidatingQuickSearchClient(displayed)
    dialog = ListarySearchDialog(client=client)
    calls: list[str] = []
    destination = _associated_open_receipt(target)

    def fail(path, **_kwargs):
        calls.append(str(path))
        raise AssociatedOpenError(
            "delivery_unverifiable",
            "fixture handler reused another process",
            ambiguous=True,
            dispatch_happened=True,
            destination_process=destination.destination_process,
            destination_window=destination.destination_window,
        )

    monkeypatch.setattr(listary_search_module, "open_file_verified", fail)
    try:
        _select_dialog_result(dialog, displayed)
        app.processEvents()
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)

        failure = dialog._last_file_manager_failure_receipt
        assert calls == [str(target)]
        assert failure is not None
        assert failure.code == "delivery_unverifiable"
        assert failure.ambiguous is True
        assert failure.dispatch_happened is True
        assert failure.destination is not None
        assert failure.destination["process"]["process_id"] == destination.destination_process.process_id
        assert failure.destination["window"]["hwnd"] == destination.destination_window.hwnd
        assert client.activation_sources == []
        assert "no retry" in dialog.status.text().lower()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_action_revalidation_reconnect_retry_is_bounded_to_one_pre_side_effect(tmp_path: Path) -> None:
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    displayed = ListarySearchResult(
        item_id=193,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
        root_id="root-a",
        stable_file_id="stable-a",
        available=True,
    )

    class _ReconnectClient:
        def __init__(self, *, recover: bool) -> None:
            self.calls = 0
            self.recover = recover

        def get_item(self, item_id: int):
            assert item_id == displayed.item_id
            self.calls += 1
            if self.calls == 1 or not self.recover:
                raise ListaryAgentError("unreachable", "fixture disconnected")
            return displayed

    recovering = _ReconnectClient(recover=True)
    current, presentation, error = listary_search_module._revalidate_result_for_action(
        recovering,
        displayed,
        refresh_label="Search again.",
    )
    assert recovering.calls == 2
    assert current == displayed
    assert presentation is not None and presentation.action_path == displayed.path
    assert error == ""

    unavailable = _ReconnectClient(recover=False)
    current, presentation, error = listary_search_module._revalidate_result_for_action(
        unavailable,
        displayed,
        refresh_label="Search again.",
    )
    assert unavailable.calls == 2
    assert current is None
    assert presentation is None
    assert "Could not revalidate" in error


def test_show_standalone_captures_native_dialog_before_opening_launcher() -> None:
    events: list[object] = []

    class _DialogStub:
        def capture_foreground_file_dialog(self):
            events.append("capture")

        def clear_file_dialog_context(self):
            events.append("clear")

        def open_for_query(self, query: str = "") -> None:
            events.append(("open", query))

    listary_search_module._show_standalone_dialog(
        _DialogStub(),
        "scene",
        capture_file_dialog=True,
    )

    assert events == ["capture", ("open", "scene")]


def test_quick_switch_retains_ambiguous_failure_after_action_generation_is_retired() -> None:
    app = QApplication.instance() or QApplication([])
    client = _QuickSearchClient()
    dialog = ListarySearchDialog(client=client)
    receipt = listary_search_module.WindowsFileDialogFailureReceipt(
        schema="listary_windows_file_dialog_failure_v1",
        outcome="failed",
        code="cancelled_after_dispatch",
        action="navigate",
        dialog_kind="open",
        context_token="windows-dialog:fixture",
        context_generation=3,
        activation_generation=8,
        query_text="scene",
        item_id=55,
        root_id="root-a",
        stable_file_id="stable-a",
        displayed_path="C:/before",
        target_path="C:/target",
        action_latency_ms=12.5,
        accepted_by_dialog=False,
        ambiguous=True,
        detail="fixture cancelled after dispatch",
    )
    error = listary_search_module.WindowsFileDialogError(
        receipt.code,
        receipt.detail,
        ambiguous=True,
        receipt=receipt,
    )
    dialog._file_dialog_action_generation = 9

    dialog._file_dialog_action_finished(object(), 8, None, error)
    app.processEvents()

    assert dialog._last_file_dialog_failure_receipt == receipt
    assert client.activations == []
    dialog.deleteLater()


@pytest.mark.parametrize("surface", ["quick", "file_search"])
def test_native_save_overwrite_pending_disables_followup_dispatch(surface: str, tmp_path: Path) -> None:
    """After Save invokes the native overwrite prompt, a second Enter must never invoke Save again."""
    from types import SimpleNamespace
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "already-exists.txt"
    target.write_bytes(b"original unchanged")
    displayed = ListarySearchResult(
        item_id=875, path=str(target), parent_path=str(tmp_path), name=target.name,
        kind="file", root_id="root-fixture", stable_file_id="fixture-stable", available=True,
    )
    class _SpyAdapter:
        calls = 0
        new_context = None
        def prepare_context_for_action(self, _context):
            self.calls += 1
            raise AssertionError("native dialog Save was repeated")
        def capture_foreground(self):
            return self.new_context

    adapter = _SpyAdapter()
    client = _RevalidatingQuickSearchClient(displayed) if surface == "quick" else _FileSearchClient([])
    dialog = (ListarySearchDialog(client=client, file_dialog_adapter=adapter) if surface == "quick"
              else ListaryFileSearchDialog(client=client, file_dialog_adapter=adapter))
    dialog._file_dialog_context = SimpleNamespace(dialog_kind="save", context_token="fixture-pending", generation=3)
    dialog._last_file_dialog_failure_receipt = listary_search_module.WindowsFileDialogFailureReceipt(
        schema="listary_windows_file_dialog_failure_v1", outcome="failed",
        code="overwrite_requires_confirmation", action="save", dialog_kind="save",
        context_token="fixture-pending", context_generation=3, activation_generation=5,
        query_text="already", item_id=875, root_id="root-fixture", stable_file_id="fixture-stable",
        displayed_path=str(target), target_path=str(target), action_latency_ms=3.0,
        accepted_by_dialog=False, ambiguous=True, detail="Windows awaits user confirmation",
        acceptance_may_have_occurred=True, dispatch_happened=True,
    )
    generation = dialog._file_dialog_action_generation
    try:
        if surface == "quick":
            _select_dialog_result(dialog, displayed)
            dialog._sync_result_state()
            assert dialog.open_button.isEnabled() is False
            assert "Enter Save disabled" in dialog.shortcut_hints.text()
            assert dialog.actions_open_action.isEnabled() is False
        assert dialog._start_file_dialog_action(displayed) is True
        assert dialog._file_dialog_action_generation == generation
        assert adapter.calls == 0
        assert "Windows" in dialog.status.text()
        assert target.read_bytes() == b"original unchanged"
        if surface == "file_search":
            dialog.set_file_dialog_context(adapter, dialog._file_dialog_context)
            assert dialog._last_file_dialog_failure_receipt is not None
        else:
            dialog._sync_result_state()
            assert dialog.actions_open_action.isEnabled() is False
        # A separately captured dialog identity retires the old pending prompt
        # without interpreting the old Save as failed or attempting a replay.
        new_context = SimpleNamespace(dialog_kind="save", context_token="fresh-dialog", generation=4)
        prior_receipt = dialog._last_file_dialog_failure_receipt
        if surface == "file_search":
            dialog.set_file_dialog_context(adapter, new_context)
        else:
            adapter.new_context = new_context
            assert dialog.capture_foreground_file_dialog() is new_context
        assert listary_search_module._native_save_confirmation_pending(
            new_context, dialog._last_file_dialog_failure_receipt
        ) is False
        assert dialog._last_file_dialog_failure_receipt is prior_receipt
        dialog._sync_result_state()
        assert "awaiting a human overwrite decision" not in dialog.open_button.toolTip()
        if surface == "quick":
            assert dialog.open_button.isEnabled() is True
        assert adapter.calls == 0
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_switch_open_file_requires_dialog_acceptance_before_activation(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "scene.txt"
    target.write_text("fixture", encoding="utf-8")
    displayed = ListarySearchResult(
        item_id=600,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
        root_id="root-a",
        stable_file_id="stable-a",
        available=True,
    )
    client = _RevalidatingQuickSearchClient(displayed)
    dialog = ListarySearchDialog(client=client)
    receipt = listary_search_module.WindowsFileDialogReceipt(
        schema="listary_windows_file_dialog_receipt_v1",
        outcome="verified",
        action="select",
        dialog_kind="open",
        context_token="windows-dialog:fixture",
        context_generation=3,
        activation_generation=8,
        query_text="scene",
        item_id=displayed.item_id,
        root_id=displayed.root_id,
        stable_file_id=displayed.stable_file_id,
        displayed_path=displayed.path,
        target_path=displayed.path,
        directory_before=str(tmp_path),
        directory_after=str(tmp_path),
        selected_path_after=displayed.path,
        action_latency_ms=12.5,
        accepted_by_dialog=False,
        ambiguous=False,
        detail="selection only",
        action_generation=8,
        result_generation=8,
        item_kind="file",
        acceptance_may_have_occurred=False,
    )
    dialog._file_dialog_action_generation = 8

    dialog._file_dialog_action_finished(object(), 8, (displayed, receipt), None)
    app.processEvents()

    assert client.activation_sources == []
    assert "did not confirm Open acceptance" in dialog.status.text()
    dialog.deleteLater()


def test_quick_switch_save_file_requires_dialog_acceptance_before_activation(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "scene.txt"
    target.write_text("fixture", encoding="utf-8")
    displayed = ListarySearchResult(
        item_id=605,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
        root_id="root-a",
        stable_file_id="stable-a",
        available=True,
    )
    client = _RevalidatingQuickSearchClient(displayed)
    dialog = ListarySearchDialog(client=client)
    receipt = listary_search_module.WindowsFileDialogReceipt(
        schema="listary_windows_file_dialog_receipt_v1",
        outcome="verified",
        action="prepare",
        dialog_kind="save",
        context_token="windows-dialog:fixture-save",
        context_generation=3,
        activation_generation=8,
        query_text="scene",
        item_id=displayed.item_id,
        root_id=displayed.root_id,
        stable_file_id=displayed.stable_file_id,
        displayed_path=displayed.path,
        target_path=displayed.path,
        directory_before=str(tmp_path),
        directory_after=str(tmp_path),
        selected_path_after=None,
        action_latency_ms=12.5,
        accepted_by_dialog=False,
        ambiguous=False,
        detail="filename prepared only",
        action_generation=8,
        result_generation=8,
        item_kind="file",
        prepared_filename_after=displayed.name,
        acceptance_may_have_occurred=False,
    )
    dialog._file_dialog_action_generation = 8

    dialog._file_dialog_action_finished(object(), 8, (displayed, receipt), None)
    app.processEvents()

    assert client.activation_sources == []
    assert "did not confirm Save acceptance" in dialog.status.text()
    dialog.deleteLater()


@pytest.mark.parametrize("surface", ["quick", "file_search"])
@pytest.mark.parametrize(
    "proof_case",
    [
        "missing-destination-proof",
        "missing-destination-file-id",
        "malformed-sha256",
        "mismatched-saved-path",
        "mismatched-item-identity",
        "mismatched-context",
        "stale-action-generation",
        "not-dispatched",
        "unchanged-artifact",
        "complete-destination-proof",
    ],
)
def test_native_save_consumer_requires_stable_destination_proof(
    surface: str,
    proof_case: str,
    tmp_path: Path,
) -> None:
    """A Save acceptance flag alone must not authorize closing without the stable artifact receipt."""
    import hashlib
    from dataclasses import replace
    from types import SimpleNamespace
    from PySide6.QtWidgets import QDialog

    app = QApplication.instance() or QApplication([])
    target = tmp_path / "verified-scene.txt"
    target.write_bytes(b"saved fixture bytes")
    displayed = ListarySearchResult(
        item_id=875,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
        root_id="root-fixture",
        stable_file_id="indexed-fixture-id",
        available=True,
    )
    if surface == "quick":
        client = _RevalidatingQuickSearchClient(displayed)
        dialog = ListarySearchDialog(client=client)
    else:
        client = _FileSearchClient([])
        dialog = ListaryFileSearchDialog(client=client, preview_broker=None)
    activations: list[tuple[int, str]] = []
    client.record_activation = lambda item_id, source="launcher": activations.append((int(item_id), str(source)))
    dialog._file_dialog_context = SimpleNamespace(
        dialog_kind="save",
        context_token="windows-dialog:fixture-save-proof",
        generation=3,
    )
    dialog._file_dialog_action_generation = 8
    dialog._generation = 8
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    proof_valid = proof_case != "missing-destination-proof"
    receipt = listary_search_module.WindowsFileDialogReceipt(
        schema="listary_windows_file_dialog_receipt_v1",
        outcome="verified",
        action="save",
        dialog_kind="save",
        context_token="windows-dialog:fixture-save-proof",
        context_generation=3,
        activation_generation=8,
        query_text="scene",
        item_id=displayed.item_id,
        root_id=displayed.root_id,
        stable_file_id=displayed.stable_file_id,
        displayed_path=displayed.path,
        target_path=str(target),
        directory_before=str(tmp_path),
        directory_after=str(tmp_path),
        selected_path_after=None,
        action_latency_ms=12.5,
        accepted_by_dialog=True,
        ambiguous=False,
        detail="fixture Save delivery receipt",
        action_generation=8,
        result_generation=8,
        item_kind="file",
        prepared_filename_after=target.name,
        acceptance_may_have_occurred=False,
        dispatch_happened=True,
        saved_path_after=str(target),
        overwrite_confirmed=False,
        artifact_changed=True,
        saved_size_bytes=target.stat().st_size,
        saved_modified_ns=target.stat().st_mtime_ns,
        saved_file_id="destination-file-id" if proof_valid else "",
        saved_sha256=digest if proof_valid else "",
        saved_hash_verified=proof_valid,
    )
    if proof_case == "missing-destination-file-id":
        receipt = replace(receipt, saved_file_id="")
    elif proof_case == "malformed-sha256":
        receipt = replace(receipt, saved_sha256="g" * 64)
    elif proof_case == "mismatched-saved-path":
        receipt = replace(receipt, saved_path_after=str(tmp_path / "different.txt"))
    elif proof_case == "mismatched-item-identity":
        receipt = replace(receipt, item_id=displayed.item_id + 1)
    elif proof_case == "mismatched-context":
        receipt = replace(receipt, context_token="windows-dialog:other-context")
    elif proof_case == "stale-action-generation":
        receipt = replace(receipt, action_generation=7)
    elif proof_case == "not-dispatched":
        receipt = replace(receipt, dispatch_happened=False)
    elif proof_case == "unchanged-artifact":
        receipt = replace(receipt, artifact_changed=False)

    try:
        dialog._file_dialog_action_finished(object(), 8, (displayed, receipt), None)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        app.processEvents()
        if proof_case == "complete-destination-proof":
            assert dialog.result() == QDialog.Accepted
            expected_source = "quick_switch" if surface == "quick" else "file_search"
            assert activations == [(displayed.item_id, expected_source)]
        else:
            assert dialog.result() != QDialog.Accepted
            assert activations == []
            assert "destination" in dialog.status.text().lower()
            assert "no successful save was claimed" in dialog.status.text().lower()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_switch_sync_context_failure_gets_exact_failure_receipt(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    displayed = ListarySearchResult(
        item_id=601,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
        root_id="root-a",
        stable_file_id="stable-a",
        available=True,
    )
    context = listary_search_module.WindowsFileDialogContext(
        schema="listary_file_context_v1",
        context_token="windows-dialog:fixture",
        generation=4,
        hwnd=101,
        process_id=202,
        thread_id=303,
        owner_hwnd=404,
        runtime_id=(42, 101),
        window_class="#32770",
        title="Open",
        dialog_kind="open",
        current_directory=str(tmp_path),
        process_start_time_ns=505,
    )

    class _PrepareFailAdapter:
        failure_receipt = staticmethod(listary_search_module.WindowsFileDialogAdapter.failure_receipt)

        def capture_foreground(self):
            return context

        def prepare_context_for_action(self, _context) -> None:
            raise listary_search_module.WindowsFileDialogError(
                "stale_context",
                "fixture dialog closed before action",
            )

    client = _RevalidatingQuickSearchClient(displayed)
    dialog = ListarySearchDialog(client=client, file_dialog_adapter=_PrepareFailAdapter())
    try:
        assert dialog.capture_foreground_file_dialog() == context
        dialog.search.setText("scene")
        dialog._debounce.stop()
        _select_dialog_result(dialog, displayed)
        app.processEvents()

        assert dialog._open_current() is True
        receipt = dialog._last_file_dialog_failure_receipt
        assert receipt is not None
        assert receipt.code == "stale_context"
        assert receipt.context_token == context.context_token
        assert receipt.item_id == displayed.item_id
        assert receipt.stable_file_id == displayed.stable_file_id
        assert receipt.displayed_path == displayed.path
        assert receipt.target_path == displayed.path
        assert receipt.accepted_by_dialog is False
        assert receipt.ambiguous is False
        assert client.get_item_calls == []
        assert client.activation_sources == []
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_switch_pre_mutation_stale_result_gets_failure_receipt(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    displayed = ListarySearchResult(
        item_id=602,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
        root_id="root-a",
        stable_file_id="stable-a",
        available=True,
    )
    context = listary_search_module.WindowsFileDialogContext(
        schema="listary_file_context_v1",
        context_token="windows-dialog:fixture-stale-result",
        generation=5,
        hwnd=111,
        process_id=222,
        thread_id=333,
        owner_hwnd=444,
        runtime_id=(42, 111),
        window_class="#32770",
        title="Open",
        dialog_kind="open",
        current_directory=str(tmp_path),
        process_start_time_ns=555,
    )

    class _StaleResultAdapter:
        failure_receipt = staticmethod(listary_search_module.WindowsFileDialogAdapter.failure_receipt)

        def capture_foreground(self):
            return context

        def prepare_context_for_action(self, _context) -> None:
            return None

        def apply_result(self, *_args, **_kwargs):
            raise AssertionError("stale result must fail before native mutation")

    client = _RevalidatingQuickSearchClient(None)
    dialog = ListarySearchDialog(client=client, file_dialog_adapter=_StaleResultAdapter())
    try:
        assert dialog.capture_foreground_file_dialog() == context
        dialog.search.setText("scene")
        dialog._debounce.stop()
        _select_dialog_result(dialog, displayed)
        app.processEvents()

        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_dialog_action_workers)
        receipt = dialog._last_file_dialog_failure_receipt
        assert receipt is not None
        assert receipt.code == "stale_result"
        assert receipt.context_token == context.context_token
        assert receipt.item_id == displayed.item_id
        assert receipt.target_path == displayed.path
        assert receipt.accepted_by_dialog is False
        assert receipt.ambiguous is False
        assert client.get_item_calls == [displayed.item_id]
        assert client.activation_sources == []
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_switch_dialog_ready_metric_stops_after_context_handoff(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    displayed = ListarySearchResult(
        item_id=603,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
        root_id="root-a",
        stable_file_id="stable-a",
        available=True,
    )
    context = listary_search_module.WindowsFileDialogContext(
        schema="listary_file_context_v1",
        context_token="windows-dialog:fixture-ready",
        generation=6,
        hwnd=121,
        process_id=232,
        thread_id=343,
        owner_hwnd=454,
        runtime_id=(42, 121),
        window_class="#32770",
        title="Open",
        dialog_kind="open",
        current_directory=str(tmp_path),
        process_start_time_ns=565,
    )

    class _ReadyAdapter:
        failure_receipt = staticmethod(listary_search_module.WindowsFileDialogAdapter.failure_receipt)

        def capture_foreground(self):
            return context

        def prepare_context_for_action(self, _context) -> None:
            time.sleep(0.02)

        def apply_result(self, *_args, **_kwargs):
            raise AssertionError("stale result must fail before native mutation")

    client = _RevalidatingQuickSearchClient(None)
    dialog = ListarySearchDialog(client=client, file_dialog_adapter=_ReadyAdapter())
    try:
        assert dialog.capture_foreground_file_dialog() == context
        dialog.search.setText("scene")
        dialog._debounce.stop()
        _select_dialog_result(dialog, displayed)
        dialog._last_result_ready_monotonic = time.perf_counter() - 0.05
        app.processEvents()

        assert dialog._open_current() is True
        ready_ms = dialog._last_result_to_dialog_ready_ms
        assert ready_ms is not None
        assert 50.0 <= ready_ms < 500.0
        _wait_until(app, lambda: not dialog._file_dialog_action_workers)
        assert dialog._last_result_to_dialog_ready_ms == ready_ms
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_sync_context_failure_gets_exact_failure_receipt(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    displayed = ListarySearchResult(
        item_id=604,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
        root_id="root-a",
        stable_file_id="stable-a",
        available=True,
    )
    context = listary_search_module.WindowsFileDialogContext(
        schema="listary_file_context_v1",
        context_token="windows-dialog:fixture-deep",
        generation=7,
        hwnd=131,
        process_id=242,
        thread_id=353,
        owner_hwnd=464,
        runtime_id=(42, 131),
        window_class="#32770",
        title="Save As",
        dialog_kind="save",
        current_directory=str(tmp_path),
        process_start_time_ns=575,
    )

    class _PrepareFailAdapter:
        failure_receipt = staticmethod(listary_search_module.WindowsFileDialogAdapter.failure_receipt)

        def prepare_context_for_action(self, _context) -> None:
            raise listary_search_module.WindowsFileDialogError(
                "stale_context",
                "fixture deep dialog closed before action",
            )

    client = _FileSearchClient([])
    client.items[displayed.item_id] = displayed
    dialog = ListaryFileSearchDialog(
        client=client,
        file_dialog_adapter=_PrepareFailAdapter(),
        file_dialog_context=context,
    )
    try:
        dialog.search.setText("scene")
        dialog._debounce.stop()
        assert dialog._start_file_dialog_action(displayed) is True
        receipt = dialog._last_file_dialog_failure_receipt
        assert receipt is not None
        assert receipt.code == "stale_context"
        assert receipt.dialog_kind == "save"
        assert receipt.action == "save"
        assert receipt.context_token == context.context_token
        assert receipt.item_id == displayed.item_id
        assert receipt.target_path == displayed.path
        assert receipt.accepted_by_dialog is False
        assert receipt.ambiguous is False
        assert client.get_item_calls == []
        assert client.activations == []
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_deep_file_search_surfaces_current_ambiguous_failure_without_retry() -> None:
    app = QApplication.instance() or QApplication([])
    client = _FileSearchClient([])
    dialog = ListaryFileSearchDialog(client=client)
    receipt = listary_search_module.WindowsFileDialogFailureReceipt(
        schema="listary_windows_file_dialog_failure_v1",
        outcome="failed",
        code="navigation_timeout",
        action="navigate",
        dialog_kind="save",
        context_token="windows-dialog:fixture",
        context_generation=4,
        activation_generation=11,
        query_text="folder",
        item_id=56,
        root_id="root-b",
        stable_file_id="stable-b",
        displayed_path="C:/before",
        target_path="C:/target",
        action_latency_ms=2000.0,
        accepted_by_dialog=False,
        ambiguous=True,
        detail="fixture timeout after dispatch",
    )
    error = listary_search_module.WindowsFileDialogError(
        receipt.code,
        receipt.detail,
        ambiguous=True,
        receipt=receipt,
    )
    dialog._file_dialog_action_generation = 12

    dialog._file_dialog_action_finished(object(), 12, None, error)
    app.processEvents()

    assert dialog._last_file_dialog_failure_receipt == receipt
    assert "state may have changed" in dialog.status.text()
    assert "no retry" in dialog.status.text()
    assert client.activations == []
    dialog.deleteLater()


def test_quick_search_shutdown_drains_inflight_activation_before_owned_client_close(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "activation.txt"
    target.write_text("fixture", encoding="utf-8")
    result = ListarySearchResult(501, str(target), str(tmp_path), target.name, "file")
    client = _BlockingActivationClient()
    monkeypatch.setattr(listary_search_module, "ListaryAgentClient", lambda: client)
    monkeypatch.setattr(
        listary_search_module,
        "open_file_verified",
        lambda path, *, revalidate_target, **_kwargs: (revalidate_target(), _associated_open_receipt(path))[1],
    )
    dialog = ListarySearchDialog()
    try:
        _select_dialog_result(dialog, result)
        app.processEvents()
        assert dialog._open_current() is True
        _wait_until(app, client.started.is_set)

        release_thread = threading.Thread(target=lambda: (time.sleep(0.05), client.release.set()), daemon=True)
        release_thread.start()
        assert dialog.shutdown(500) is True
        release_thread.join(timeout=1.0)

        assert client.finished.is_set() is True
        assert client.activations == [(501, "launcher")]
        assert client.closed is True
        assert client.closed_before_finished is False
    finally:
        client.release.set()
        QThreadPool.globalInstance().waitForDone(2_000)
        dialog.deleteLater()


def test_file_search_shutdown_drains_inflight_activation_before_owned_client_close(monkeypatch) -> None:
    QApplication.instance() or QApplication([])
    client = _BlockingActivationClient()
    monkeypatch.setattr(listary_search_module, "ListaryAgentClient", lambda: client)
    dialog = ListaryFileSearchDialog()
    try:
        dialog._record_file_search_activations((601,))
        assert client.started.wait(1.0) is True

        release_thread = threading.Thread(target=lambda: (time.sleep(0.05), client.release.set()), daemon=True)
        release_thread.start()
        assert dialog.shutdown(500) is True
        release_thread.join(timeout=1.0)

        assert client.finished.is_set() is True
        assert client.activations == [(601, "file_search")]
        assert client.closed is True
        assert client.closed_before_finished is False
    finally:
        client.release.set()
        QThreadPool.globalInstance().waitForDone(2_000)
        dialog.deleteLater()


def test_quick_search_shutdown_is_bounded_when_activation_does_not_finish(monkeypatch) -> None:
    QApplication.instance() or QApplication([])
    client = _BlockingActivationClient()
    monkeypatch.setattr(listary_search_module, "ListaryAgentClient", lambda: client)
    dialog = ListarySearchDialog()
    try:
        listary_search_module._start_tracked_one_shot(
            dialog._activation_workers,
            lambda: client.record_activation(701),
        )
        assert client.started.wait(1.0) is True
        started = time.perf_counter()
        assert dialog.shutdown(30) is False
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        assert elapsed_ms < 500.0
        assert client.closed is True
        assert client.closed_before_finished is True
    finally:
        client.release.set()
        assert client.finished.wait(1.0) is True
        QThreadPool.globalInstance().waitForDone(2_000)
        dialog.deleteLater()


def test_activation_shutdown_does_not_wait_unrelated_global_thread_pool_work(monkeypatch) -> None:
    QApplication.instance() or QApplication([])
    client = _BlockingActivationClient()
    client.release.set()
    monkeypatch.setattr(listary_search_module, "ListaryAgentClient", lambda: client)
    dialog = ListarySearchDialog()
    unrelated_started = threading.Event()
    unrelated_release = threading.Event()

    def unrelated_work() -> None:
        unrelated_started.set()
        unrelated_release.wait(2.0)

    try:
        listary_search_module._start_tracked_one_shot(
            dialog._activation_workers,
            lambda: client.record_activation(801),
        )
        assert client.finished.wait(1.0) is True
        QThreadPool.globalInstance().start(listary_search_module._OneShotWorker(unrelated_work))
        assert unrelated_started.wait(1.0) is True

        assert dialog.shutdown(500) is True
        assert unrelated_release.is_set() is False
        assert client.closed is True
        assert client.closed_before_finished is False
    finally:
        unrelated_release.set()
        QThreadPool.globalInstance().waitForDone(2_000)
        dialog.deleteLater()


def test_result_action_failures_keep_dialog_open_and_surface_status(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    target = tmp_path / "gone.txt"
    result = ListarySearchResult(
        item_id=11,
        path=str(target),
        parent_path=str(tmp_path),
        name=target.name,
        kind="file",
    )
    dialog = ListarySearchDialog(client=_ActionClient())
    try:
        _select_dialog_result(dialog, result)
        app.processEvents()
        # Windows file/folder Reveal is intentionally owned by the newer
        # asynchronous verified-Explorer path and has dedicated failure/currentness
        # coverage above.  This legacy case verifies the synchronous fallback
        # helper contract used by non-Windows shells.
        monkeypatch.setattr(listary_search_module.sys, "platform", "linux")
        monkeypatch.setattr(listary_search_module, "reveal_in_file_manager", lambda _path: False)
        assert dialog._reveal_current() is False
        assert dialog.status.text() == f"Could not reveal {target.name}."

        monkeypatch.setattr(listary_search_module, "open_path", lambda _path: False)
        assert dialog._open_current() is False
        assert dialog.status.text() == f"Could not open {target.name}."
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_search_field_keyboard_navigation_and_direct_result_hotkey(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    client = _ActionClient()
    dialog = ListarySearchDialog(client=client)
    first = ListarySearchResult(101, str(tmp_path / "one.txt"), str(tmp_path), "one.txt", "file")
    second = ListarySearchResult(102, str(tmp_path / "two.txt"), str(tmp_path), "two.txt", "file")
    opened: list[str] = []

    def verified(path, *, revalidate_target, **_kwargs):
        revalidate_target()
        opened.append(str(path))
        return _associated_open_receipt(path)

    monkeypatch.setattr(listary_search_module, "open_file_verified", verified)
    try:
        _select_dialog_result(dialog, first)
        _select_dialog_result(dialog, second)
        dialog.results.setCurrentRow(0)
        dialog.show()
        dialog.search.setFocus()
        app.processEvents()

        QTest.keyClick(dialog.search, Qt.Key.Key_Down)
        app.processEvents()
        assert dialog.results.currentRow() == 1
        QTest.keyClick(dialog.search, Qt.Key.Key_Up)
        app.processEvents()
        assert dialog.results.currentRow() == 0

        QTest.keyClick(dialog.search, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier)
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert opened == [str(tmp_path / "two.txt")]
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert client.activations == [102]
    finally:
        dialog.deleteLater()


def test_quick_launcher_uses_nine_result_budget_and_compacts_to_visible_rows(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    client = _QuickSearchClient()
    dialog = ListarySearchDialog(client=client)
    try:
        dialog._start_search()
        _wait_until(app, lambda: not dialog._workers)
        assert client.search_limits == [9]

        nine_results = tuple(
            ListarySearchResult(
                200 + index,
                str(tmp_path / f"result-{index}.txt"),
                str(tmp_path),
                f"result-{index}.txt",
                "file",
            )
            for index in range(9)
        )
        dialog.search.setText("result")
        dialog._debounce.stop()
        dialog._search_finished(None, dialog._search_generation, ListarySearchPage(nine_results), None)
        app.processEvents()
        nine_height = dialog.height()
        assert dialog.results.count() == 9
        assert "Ctrl+1..9 Open" in dialog.shortcut_hints.text()
        assert dialog.width() >= 760

        three_results = nine_results[:3]
        dialog._search_finished(None, dialog._search_generation, ListarySearchPage(three_results), None)
        app.processEvents()
        assert dialog.results.count() == 3
        assert dialog.height() < nine_height
        assert dialog.height() >= dialog.minimumHeight()
    finally:
        dialog.deleteLater()


def test_quick_launcher_footer_uses_menu_options_actions_without_losing_action_state(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    client = _ActionClient()
    dialog = ListarySearchDialog(client=client)
    result = ListarySearchResult(311, str(tmp_path / "footer.txt"), str(tmp_path), "footer.txt", "file")
    try:
        dialog.show()
        _select_dialog_result(dialog, result)
        app.processEvents()

        assert dialog.menu_button.isVisible() is True
        assert dialog.file_search_button.isVisible() is True
        assert dialog.options_button.isVisible() is True
        assert dialog.actions_button.isVisible() is True
        assert dialog.index_button.isVisible() is False
        assert dialog.manage_roots_button.isVisible() is False
        assert dialog.open_button.isVisible() is False
        assert [action.text() for action in dialog.options_menu.actions() if not action.isSeparator()] == [
            "Index Folder...",
            "Indexed Folders...",
            "File Search Filters...",
            "Appearance",
        ]
        assert [action.text() for action in dialog.actions_menu.actions() if not action.isSeparator()] == [
            "Open",
            "Reveal",
            "Copy Path",
            "Pin / Unpin",
        ]
        assert dialog.actions_open_action.isEnabled() is True
        assert dialog.actions_reveal_action.isEnabled() is True
        assert dialog.actions_copy_action.isEnabled() is True
        assert dialog.actions_pin_action.isEnabled() is True

        dialog.results.clearSelection()
        dialog.results.setCurrentRow(-1)
        dialog._sync_result_state()
        assert dialog.actions_open_action.isEnabled() is False
        assert dialog.actions_reveal_action.isEnabled() is False
        assert dialog.actions_copy_action.isEnabled() is False
        assert dialog.actions_pin_action.isEnabled() is False
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize(
    ("kind", "expected_action"),
    (("folder", "Save in Selected Folder"), ("file", "Save / Replace Selected File")),
)
def test_quick_launcher_save_action_names_the_actual_native_commit(
    tmp_path: Path, kind: str, expected_action: str,
) -> None:
    """The Save dialog's Enter action commits a file, not a folder-only navigation."""
    app = QApplication.instance() or QApplication([])
    path = tmp_path / ("destination" if kind == "folder" else "existing.txt")
    if kind == "folder":
        path.mkdir()
    else:
        path.write_text("original", encoding="utf-8")
    result = ListarySearchResult(313, str(path), str(tmp_path), path.name, kind)
    dialog = ListarySearchDialog(client=_ActionClient())
    try:
        dialog._file_dialog_context = type("BoundSave", (), {"dialog_kind": "save"})()
        _select_dialog_result(dialog, result)
        dialog._sync_result_state()
        app.processEvents()
        assert dialog.actions_open_action.text() == expected_action
        assert "Enter Save" in dialog.shortcut_hints.text()
        assert dialog.open_button.accessibleName() == expected_action
    finally:
        dialog.deleteLater()


def _workspace_application_hint(tmp_path: Path, **overrides) -> str:
    payload = {
        "schema": "workspace_catalog_launch_target_v1",
        "source_provider": "workspace_catalog:ss-scripts",
        "workspace_id": "ss-scripts",
        "system_id": "spiffy_workspace",
        "launch_target_id": "visual_clipboard",
        "label": "Spiffy Visual Clipboard",
        "aliases": ["clipboard", "visual clipboard"],
        "launch_path": str((tmp_path / "Launch Visual Clipboard.bat").resolve()),
    }
    payload.update(overrides)
    return "workspace_application_v1:" + json.dumps(payload, separators=(",", ":"))


class _RootAwareQuickSearchClient(_QuickSearchClient):
    def __init__(self, page: ListarySearchPage, roots: tuple[dict, ...]) -> None:
        super().__init__()
        self.page = page
        self.roots = tuple(dict(root) for root in roots)
        self.root_reads = 0

    def search(self, query: str, **kwargs) -> ListarySearchPage:
        self.searches.append(str(query))
        self.search_limits.append(int(kwargs.get("limit", 40)))
        return self.page

    def list_roots(self):
        self.root_reads += 1
        return tuple(dict(root) for root in self.roots)


def test_quick_launcher_rootless_provider_rows_expose_index_recovery_without_hiding_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    launch_path = (tmp_path / "Launch Visual Clipboard.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    provider = ListarySearchResult(
        item_id=801,
        path=str(tmp_path / "visual_clipboard.workspaceapp"),
        parent_path=str(tmp_path),
        name="visual_clipboard.workspaceapp",
        kind="app",
        preview_hint=_workspace_application_hint(tmp_path, launch_path=str(launch_path)),
    )
    client = _RootAwareQuickSearchClient(ListarySearchPage((provider,), elapsed_micros=125), ())
    dialog = ListarySearchDialog(client=client)
    opened: list[str] = []
    monkeypatch.setattr(dialog, "_choose_index_folder", lambda: opened.append("index"))
    try:
        dialog.open_for_query("spiffy")
        _wait_until(app, lambda: not dialog._workers)
        app.processEvents()

        assert dialog.results.count() == 1
        assert dialog._current_result() == provider
        assert client.root_reads == 1
        assert "No indexed folders yet" in dialog.status.text()
        assert "Options > Index Folder..." in dialog.status.text()
        assert dialog.root_recovery_button.isVisible()
        assert dialog.root_recovery_button.text() == "Index a folder..."
        assert "Add a folder" in dialog.root_recovery_button.accessibleName()
        assert opened == []
        dialog.root_recovery_button.click()
        assert opened == ["index"]
        assert dialog.search.hasFocus() is True
        assert dialog.actions_open_action.isEnabled() is True
        dialog._search_finished(None, dialog._search_generation, None, RuntimeError("Agent unavailable"))
        assert not dialog.root_recovery_button.isVisible()
        assert "Agent unavailable" in dialog.status.text()
        assert dialog.actions_open_action.isEnabled() is False
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_launcher_rootless_empty_query_exposes_recovery_without_creating_a_root() -> None:
    app = QApplication.instance() or QApplication([])
    client = _RootAwareQuickSearchClient(ListarySearchPage((), elapsed_micros=75), ())
    dialog = ListarySearchDialog(client=client)
    try:
        dialog.open_for_query("missing file")
        _wait_until(app, lambda: not dialog._workers)
        app.processEvents()

        assert dialog.results.count() == 0
        assert client.root_reads == 1
        assert "No indexed folders yet" in dialog.status.text()
        assert "File search is not configured yet" in dialog.details.text()
        assert dialog.root_recovery_button.isVisible()
        assert dialog.actions_open_action.isEnabled() is False
        assert dialog.search.hasFocus() is True
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_launcher_disabled_roots_expose_existing_root_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    roots = ({"path": r"C:\Users\ExampleUser\Documents", "enabled": False},)
    client = _RootAwareQuickSearchClient(ListarySearchPage(()), roots)
    dialog = ListarySearchDialog(client=client)
    opened: list[str] = []
    monkeypatch.setattr(dialog, "_open_root_management", lambda: opened.append("manage"))
    try:
        dialog.open_for_query("missing file")
        _wait_until(app, lambda: not dialog._workers)
        app.processEvents()

        assert client.root_reads == 1
        assert "Indexed folders are disabled or unavailable" in dialog.status.text()
        assert "Options > Indexed Folders..." in dialog.status.text()
        assert "No indexed folder is currently available" in dialog.details.text()
        assert dialog.root_recovery_button.isVisible()
        assert dialog.root_recovery_button.text() == "Review indexed folders..."
        dialog.root_recovery_button.click()
        assert opened == ["manage"]
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_launcher_unavailable_reconciled_root_exposes_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    app = QApplication.instance() or QApplication([])
    roots = ({
        "path": r"C:\Unavailable\Device",
        "enabled": True,
        "availability": "unavailable",
        "last_reconciled_unix_ms": 1,
    },)
    client = _RootAwareQuickSearchClient(ListarySearchPage(()), roots)
    dialog = ListarySearchDialog(client=client)
    visited: list[str] = []
    monkeypatch.setattr(dialog, "_open_root_management", lambda: visited.append("manage"))
    try:
        dialog.open_for_query("no result")
        _wait_until(app, lambda: not dialog._workers)
        app.processEvents()
        assert listary_search_module._indexed_root_state(client) == (1, 0)
        assert "disabled or unavailable" in dialog.status.text()
        assert dialog.root_recovery_button.isVisible()
        assert dialog.root_recovery_button.text() == "Review indexed folders..."
        assert dialog.actions_open_action.isEnabled() is False
        dialog.root_recovery_button.click()
        assert visited == ["manage"]
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_quick_launcher_normal_indexed_result_skips_extra_root_status_call(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    result = ListarySearchResult(
        item_id=802,
        path=str(tmp_path / "needle.txt"),
        parent_path=str(tmp_path),
        name="needle.txt",
        kind="file",
    )
    client = _RootAwareQuickSearchClient(ListarySearchPage((result,), elapsed_micros=50), ())
    dialog = ListarySearchDialog(client=client)
    try:
        dialog.open_for_query("needle")
        _wait_until(app, lambda: not dialog._workers)
        app.processEvents()

        assert dialog.results.count() == 1
        assert not dialog.root_recovery_button.isVisible()
        assert client.root_reads == 0
        assert dialog.status.text() == "1 result in 0.1 ms"
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_workspace_application_preview_hint_decodes_exact_core_contract(tmp_path: Path) -> None:
    hint = _workspace_application_hint(tmp_path)
    metadata = listary_search_module.decode_workspace_application_preview_hint(hint)
    assert metadata is not None
    assert metadata.workspace_id == "ss-scripts"
    assert metadata.source_provider == "workspace_catalog:ss-scripts"
    assert metadata.label == "Spiffy Visual Clipboard"
    assert metadata.aliases == ("clipboard", "visual clipboard")
    assert metadata.launch_path == str((tmp_path / "Launch Visual Clipboard.bat").resolve())
    assert listary_search_module.decode_workspace_application_preview_hint("video") is None


def test_video_workspace_application_requires_canonical_video_row_and_launcher(tmp_path: Path) -> None:
    launch_path = (tmp_path / "Launch Spiffy Workspace.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    result = ListarySearchResult(
        item_id=904,
        path=str(tmp_path / "spiffy_video_editor.workspaceapp"),
        parent_path=str(tmp_path),
        name="spiffy_video_editor.workspaceapp",
        kind="app",
        preview_hint=_workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_video_editor",
            label="Spiffy Video Editor",
            aliases=["Video Editor"],
            launch_path=str(launch_path),
        ),
    )
    metadata = listary_search_module._video_workspace_application(result)
    assert metadata is not None
    assert metadata.launch_target_id == "spiffy_video_editor"
    assert Path(metadata.launch_path) == launch_path


def test_video_workspace_warm_success_reuses_app_owned_owner_without_bat(tmp_path: Path, monkeypatch) -> None:
    from spiffy_ui import video_editor_workspace_ipc as video_ipc

    launch_path = (tmp_path / "Launch Spiffy Workspace.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_video_editor",
            label="Spiffy Video Editor",
            aliases=["Video Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    request_ids: list[str] = []
    dispatched: list[str] = []

    def completed(request, **_kwargs):
        request_ids.append(str(request["request_id"]))
        return {
            "schema": video_ipc.VIDEO_WORKSPACE_RESPONSE_SCHEMA,
            "protocol_version": video_ipc.VIDEO_WORKSPACE_PROTOCOL_VERSION,
            "request_id": request["request_id"],
            "action": video_ipc.VIDEO_WORKSPACE_ACTION,
            "destination": video_ipc.VIDEO_WORKSPACE_DESTINATION,
            "state": "final",
            "accepted": True,
            "final": True,
            "ok": True,
            "outcome": "focused_existing",
            "window_visible": True,
            "owner_instance_id": "fixture-owner",
            "endpoint_contacted": True,
            "request_dispatched": True,
        }

    monkeypatch.setattr(video_ipc, "send_video_workspace_request", completed)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_video_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    receipt = listary_search_module._launch_video_workspace_verified(
        metadata,
        cancel_event=threading.Event(),
        revalidate_target=lambda: None,
        timeout_seconds=1.0,
    )
    assert receipt.delivery_mode == "warm_ipc"
    assert receipt.dispatch_happened is False
    assert request_ids == [receipt.request_id]
    assert dispatched == []
    assert receipt.destination["window_visible"] is True


def test_video_workspace_cold_success_dispatches_canonical_bat_once_then_same_request(tmp_path: Path, monkeypatch) -> None:
    from spiffy_ui import video_editor_workspace_ipc as video_ipc

    launch_path = (tmp_path / "Launch Spiffy Workspace.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_video_editor",
            label="Spiffy Video Editor",
            aliases=["Video Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    request_ids: list[str] = []
    dispatched: list[str] = []
    revalidations: list[int] = []

    def absent_then_completed(request, **_kwargs):
        request_ids.append(str(request["request_id"]))
        if len(request_ids) == 1:
            return {
                "ok": False,
                "error": "editor_not_running",
                "endpoint_contacted": False,
                "request_dispatched": False,
                "accepted": False,
                "final": False,
                "ambiguous": False,
                "cold_launch_eligible": True,
            }
        return {
            "schema": video_ipc.VIDEO_WORKSPACE_RESPONSE_SCHEMA,
            "protocol_version": video_ipc.VIDEO_WORKSPACE_PROTOCOL_VERSION,
            "request_id": request["request_id"],
            "action": video_ipc.VIDEO_WORKSPACE_ACTION,
            "destination": video_ipc.VIDEO_WORKSPACE_DESTINATION,
            "state": "final",
            "accepted": True,
            "final": True,
            "ok": True,
            "outcome": "focused_existing",
            "window_visible": True,
            "owner_instance_id": "fixture-owner",
            "endpoint_contacted": True,
            "request_dispatched": True,
        }

    monkeypatch.setattr(video_ipc, "send_video_workspace_request", absent_then_completed)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_video_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    receipt = listary_search_module._launch_video_workspace_verified(
        metadata,
        cancel_event=threading.Event(),
        revalidate_target=lambda: revalidations.append(len(revalidations)),
        timeout_seconds=1.0,
    )
    assert receipt.delivery_mode == "cold_launcher"
    assert receipt.dispatch_happened is True
    assert dispatched == [str(launch_path)]
    assert len(request_ids) == 2
    assert len(set(request_ids)) == 1
    assert request_ids[0] == receipt.request_id
    assert len(revalidations) >= 3


def test_video_workspace_contacted_owner_timeout_before_acceptance_never_dispatches_bat(tmp_path: Path, monkeypatch) -> None:
    from spiffy_ui import video_editor_workspace_ipc as video_ipc

    launch_path = (tmp_path / "Launch Spiffy Workspace.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_video_editor",
            label="Spiffy Video Editor",
            aliases=["Video Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    dispatched: list[str] = []
    monkeypatch.setattr(
        video_ipc,
        "send_video_workspace_request",
        lambda _request, **_kwargs: {
            "ok": False,
            "error": "ipc_accept_timeout",
            "endpoint_contacted": True,
            "request_dispatched": True,
            "accepted": False,
            "final": False,
            "ambiguous": True,
            "cold_launch_eligible": False,
        },
    )
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_video_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_video_workspace_verified(
            metadata,
            cancel_event=threading.Event(),
            revalidate_target=lambda: None,
            timeout_seconds=0.5,
        )
    assert captured.value.code == "ipc_accept_timeout"
    assert captured.value.ambiguous is True
    assert captured.value.dispatch_happened is False
    assert dispatched == []


def test_video_workspace_owner_disconnect_after_acceptance_is_ambiguous_and_never_dispatches_bat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from spiffy_ui import video_editor_workspace_ipc as video_ipc

    launch_path = (tmp_path / "Launch Spiffy Workspace.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_video_editor",
            label="Spiffy Video Editor",
            aliases=["Video Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    dispatched: list[str] = []
    monkeypatch.setattr(
        video_ipc,
        "send_video_workspace_request",
        lambda _request, **_kwargs: {
            "ok": False,
            "error": "owner_disconnected_after_acceptance",
            "endpoint_contacted": True,
            "request_dispatched": True,
            "accepted": True,
            "final": False,
            "ambiguous": True,
            "cold_launch_eligible": False,
            "accepted_response": {
                "state": "accepted",
                "accepted": True,
                "final": False,
                "owner_instance_id": "crashed-owner",
            },
        },
    )
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_video_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_video_workspace_verified(
            metadata,
            cancel_event=threading.Event(),
            revalidate_target=lambda: None,
            timeout_seconds=0.5,
        )
    assert captured.value.code == "owner_disconnected_after_acceptance"
    assert captured.value.ambiguous is True
    assert captured.value.dispatch_happened is False
    assert dispatched == []


def test_video_workspace_timeout_after_cold_acceptance_never_replays_bat(tmp_path: Path, monkeypatch) -> None:
    from spiffy_ui import video_editor_workspace_ipc as video_ipc

    launch_path = (tmp_path / "Launch Spiffy Workspace.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_video_editor",
            label="Spiffy Video Editor",
            aliases=["Video Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    request_ids: list[str] = []
    dispatched: list[str] = []

    def absent_then_accepted_timeout(request, **_kwargs):
        request_ids.append(str(request["request_id"]))
        if len(request_ids) == 1:
            return {
                "ok": False,
                "error": "editor_not_running",
                "endpoint_contacted": False,
                "request_dispatched": False,
                "accepted": False,
                "final": False,
                "ambiguous": False,
                "cold_launch_eligible": True,
            }
        return {
            "ok": False,
            "error": "ipc_final_timeout",
            "endpoint_contacted": True,
            "request_dispatched": True,
            "accepted": True,
            "final": False,
            "ambiguous": True,
            "cold_launch_eligible": False,
        }

    monkeypatch.setattr(video_ipc, "send_video_workspace_request", absent_then_accepted_timeout)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_video_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_video_workspace_verified(
            metadata,
            cancel_event=threading.Event(),
            revalidate_target=lambda: None,
            timeout_seconds=1.0,
        )
    assert captured.value.code == "ipc_final_timeout"
    assert captured.value.ambiguous is True
    assert captured.value.dispatch_happened is True
    assert dispatched == [str(launch_path)]
    assert len(request_ids) == 2
    assert len(set(request_ids)) == 1


def test_video_workspace_cancel_after_cold_dispatch_never_replays_bat(tmp_path: Path, monkeypatch) -> None:
    from spiffy_ui import video_editor_workspace_ipc as video_ipc

    launch_path = (tmp_path / "Launch Spiffy Workspace.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_video_editor",
            label="Spiffy Video Editor",
            aliases=["Video Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    cancel = threading.Event()
    dispatched: list[str] = []
    request_ids: list[str] = []

    def absent(request, **_kwargs):
        request_ids.append(str(request["request_id"]))
        return {
            "ok": False,
            "error": "editor_not_running",
            "endpoint_contacted": False,
            "request_dispatched": False,
            "accepted": False,
            "final": False,
            "ambiguous": False,
            "cold_launch_eligible": True,
        }

    def dispatch_once(path):
        dispatched.append(str(path))
        cancel.set()
        return True

    monkeypatch.setattr(video_ipc, "send_video_workspace_request", absent)
    monkeypatch.setattr(listary_search_module, "_dispatch_video_workspace_launcher", dispatch_once)
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_video_workspace_verified(
            metadata,
            cancel_event=cancel,
            revalidate_target=lambda: None,
            timeout_seconds=1.0,
        )
    assert captured.value.code == "cancelled"
    assert captured.value.ambiguous is True
    assert captured.value.dispatch_happened is True
    assert dispatched == [str(launch_path)]
    assert len(request_ids) == 1


def test_video_workspace_quick_search_wrapper_records_activation_only_after_final_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from spiffy_ui import video_editor_workspace_ipc as video_ipc

    app = QApplication.instance() or QApplication([])
    launch_path = (tmp_path / "Launch Spiffy Workspace.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    synthetic = (
        tmp_path
        / "providers"
        / "workspace_catalog"
        / "ss-scripts"
        / "spiffy_workspace"
        / "spiffy_video_editor.workspaceapp"
    )
    result = ListarySearchResult(
        item_id=309,
        path=str(synthetic),
        parent_path=str(synthetic.parent),
        name=synthetic.name,
        kind="app",
        preview_hint=_workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_video_editor",
            label="Spiffy Video Editor",
            aliases=["Video Editor"],
            launch_path=str(launch_path),
        ),
    )
    client = _RevalidatingQuickSearchClient(result)
    dialog = ListarySearchDialog(client=client)
    requests: list[dict[str, object]] = []
    dispatched: list[str] = []

    def completed_focus(request, **_kwargs):
        requests.append(dict(request))
        return {
            "schema": video_ipc.VIDEO_WORKSPACE_RESPONSE_SCHEMA,
            "protocol_version": video_ipc.VIDEO_WORKSPACE_PROTOCOL_VERSION,
            "request_id": request["request_id"],
            "action": video_ipc.VIDEO_WORKSPACE_ACTION,
            "destination": video_ipc.VIDEO_WORKSPACE_DESTINATION,
            "state": "final",
            "accepted": True,
            "final": True,
            "ok": True,
            "outcome": "focused_existing",
            "window_visible": True,
            "owner_instance_id": "video-wrapper-owner",
            "owner_pid": 3090,
            "endpoint_contacted": True,
            "request_dispatched": True,
            "cold_launch_eligible": False,
        }

    monkeypatch.setattr(video_ipc, "send_video_workspace_request", completed_focus)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_video_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    try:
        dialog._search_finished(None, 0, listary_search_module.ListarySearchPage((result,), 0, 1), None)
        app.processEvents()
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert dispatched == []
        assert len(requests) == 1
        assert client.activation_sources == [(309, "launcher")]
        receipt = dialog._last_file_manager_receipt
        assert receipt is not None
        assert isinstance(receipt.destination, listary_search_module.WorkspaceApplicationLaunchReceipt)
        assert receipt.destination.delivery_mode == "warm_ipc"
        assert receipt.destination.dispatch_happened is False
        assert receipt.destination.requested_creative_view == "video"
        assert "Verified Spiffy Video Editor" in dialog.status.text()
    finally:
        dialog.deleteLater()


def test_deep_file_search_preview_never_probes_workspace_provider_synthetic_path(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    launcher = tmp_path / "Launch Visual Clipboard.bat"
    launcher.write_text("@echo off\r\n", encoding="utf-8")
    result = ListarySearchResult(
        item_id=903,
        path=str(tmp_path / "visual-clipboard.workspaceapp"),
        parent_path=str(tmp_path),
        name="visual-clipboard.workspaceapp",
        kind="app",
        preview_hint=_workspace_application_hint(tmp_path),
    )
    client = _FileSearchClient([ListaryFileSearchPage((result,), 1, 0, 100, False, 18, 1, 100)])
    broker = _PreviewBroker()
    dialog = ListaryFileSearchDialog(client=client, preview_broker=broker)
    try:
        dialog._start_search()
        _wait_file_search(app, dialog)
        dialog._preview_timer.stop()
        assert broker.requests == []
        assert "Workspace applications use their validated launcher" in dialog.preview_status.text()
    finally:
        dialog.shutdown()
        dialog.deleteLater()


def test_workspace_application_preview_hint_rejects_malformed_or_extended_metadata(tmp_path: Path) -> None:
    malformed = (
        "workspace_application_v1:"
        + json.dumps(
            {
                "schema": "workspace_catalog_launch_target_v1",
                "source_provider": "workspace_catalog:ss-scripts",
                "workspace_id": "ss-scripts",
                "system_id": "spiffy_workspace",
                "launch_target_id": "visual_clipboard",
                "label": "Spiffy Visual Clipboard",
                "aliases": [],
                "launch_path": str((tmp_path / "Launch Visual Clipboard.bat").resolve()),
                "unexpected": True,
            }
        )
    )
    try:
        listary_search_module.decode_workspace_application_preview_hint(malformed)
    except ValueError as exc:
        assert "fields" in str(exc)
    else:
        raise AssertionError("extended workspace application metadata was accepted")

    mismatched_provider = _workspace_application_hint(tmp_path, source_provider="workspace_catalog:other")
    try:
        listary_search_module.decode_workspace_application_preview_hint(mismatched_provider)
    except ValueError as exc:
        assert "source_provider" in str(exc)
    else:
        raise AssertionError("mismatched workspace application provider was accepted")


def test_workspace_application_row_uses_label_and_routes_actions_to_launch_path(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    synthetic = tmp_path / "providers" / "workspace_catalog" / "ss-scripts" / "spiffy_workspace" / "visual_clipboard.workspaceapp"
    launch_path = (tmp_path / "Launch Visual Clipboard.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    result = ListarySearchResult(
        item_id=301,
        path=str(synthetic),
        parent_path=str(synthetic.parent),
        name=synthetic.name,
        kind="app",
        preview_hint=_workspace_application_hint(tmp_path),
    )
    client = _ActionClient()
    dialog = ListarySearchDialog(client=client)
    opened: list[str] = []
    revealed: list[str] = []
    monkeypatch.setattr(listary_search_module, "open_path", lambda path: opened.append(str(path)) or True)
    monkeypatch.setattr(listary_search_module, "reveal_in_file_manager", lambda path: revealed.append(str(path)) or True)
    try:
        dialog._search_finished(None, 0, listary_search_module.ListarySearchPage((result,), 0, 1), None)
        app.processEvents()
        row = dialog.results.item(0)
        assert row is not None
        assert row.text().splitlines()[0] == "Spiffy Visual Clipboard"
        assert row.text().splitlines()[1] == str(launch_path.parent)
        assert str(synthetic) not in row.text()
        assert row.toolTip() == str(launch_path)

        assert dialog._reveal_current() is True
        assert revealed == [str(launch_path)]
        assert dialog._copy_current_path() is True
        assert QApplication.clipboard().text() == str(launch_path)
        assert dialog._open_current() is True
        assert opened == [str(launch_path)]
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert client.activations == []
        assert "without a verifiable destination receipt" in dialog.status.text()
        assert "activation history was not changed" in dialog.status.text()
    finally:
        dialog.deleteLater()


def test_storyboard_workspace_app_warm_receipt_records_launcher_activation(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    app = QApplication.instance() or QApplication([])
    launch_path = (tmp_path / "Launch Spiffy Storyboard.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    synthetic = tmp_path / "providers" / "workspace_catalog" / "ss-scripts" / "spiffy_workspace" / "spiffy_storyboard.workspaceapp"
    result = ListarySearchResult(
        item_id=304,
        path=str(synthetic),
        parent_path=str(synthetic.parent),
        name=synthetic.name,
        kind="app",
        preview_hint=_workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_storyboard",
            label="Spiffy Storyboard",
            aliases=["storyboard"],
            launch_path=str(launch_path),
        ),
    )
    client = _RevalidatingQuickSearchClient(result)
    dialog = ListarySearchDialog(client=client)
    requests: list[dict[str, object]] = []
    opened: list[str] = []

    def completed_focus(request, **_kwargs):
        requests.append(dict(request))
        return {
            "schema": clipboard_ipc.IPC_SCHEMA_V2,
            "protocol_version": clipboard_ipc.IPC_PROTOCOL_VERSION,
            "action": "focus_view",
            "request_id": request["request_id"],
            "requested_creative_view": "storyboard",
            "accepted": True,
            "completed": True,
            "ok": True,
            "outcome": "focused_existing",
            "window_visible": True,
            "endpoint_contacted": True,
            "cold_launch_eligible": False,
        }

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", completed_focus)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_storyboard_workspace_launcher",
        lambda path: opened.append(str(path)) or True,
    )
    try:
        dialog._search_finished(None, 0, listary_search_module.ListarySearchPage((result,), 0, 1), None)
        app.processEvents()
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert opened == []
        assert len(requests) == 1
        assert requests[0]["action"] == "focus_view"
        assert requests[0]["requested_creative_view"] == "storyboard"
        assert client.activation_sources == [(304, "launcher")]
        receipt = dialog._last_file_manager_receipt
        assert receipt is not None
        assert isinstance(receipt.destination, listary_search_module.WorkspaceApplicationLaunchReceipt)
        assert receipt.destination.delivery_mode == "warm_ipc"
        assert receipt.destination.dispatch_happened is False
        assert "Verified Spiffy Storyboard" in dialog.status.text()
    finally:
        dialog.deleteLater()


def test_storyboard_verified_route_requires_canonical_workspace_provider(tmp_path: Path) -> None:
    launch_path = (tmp_path / "Launch Spiffy Storyboard.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    synthetic = tmp_path / "providers" / "workspace_catalog" / "other" / "spiffy_workspace" / "spiffy_storyboard.workspaceapp"
    result = ListarySearchResult(
        item_id=1304,
        path=str(synthetic),
        parent_path=str(synthetic.parent),
        name=synthetic.name,
        kind="app",
        preview_hint=_workspace_application_hint(
            tmp_path,
            workspace_id="other",
            source_provider="workspace_catalog:other",
            launch_target_id="spiffy_storyboard",
            label="Spiffy Storyboard",
            aliases=["storyboard"],
            launch_path=str(launch_path),
        ),
    )
    assert listary_search_module.decode_workspace_application_preview_hint(result.preview_hint) is not None
    assert listary_search_module._storyboard_workspace_application(result) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd.exe launcher contract")
def test_storyboard_launcher_dispatch_uses_exact_cmd_argv_without_shell(tmp_path: Path, monkeypatch) -> None:
    launch_path = (tmp_path / "Launch Spiffy Storyboard.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    cmd_path = (tmp_path / "cmd.exe").resolve()
    cmd_path.write_bytes(b"MZ")
    launched: list[tuple[list[str], dict[str, object]]] = []

    class _FakeProcess:
        pass

    def fake_popen(argv, **kwargs):
        launched.append(([str(part) for part in argv], dict(kwargs)))
        return _FakeProcess()

    monkeypatch.setenv("COMSPEC", str(cmd_path))
    monkeypatch.setattr(listary_search_module.subprocess, "Popen", fake_popen)
    assert listary_search_module._dispatch_storyboard_workspace_launcher(str(launch_path)) is True
    assert len(launched) == 1
    argv, kwargs = launched[0]
    assert argv == [str(cmd_path), "/d", "/c", str(launch_path)]
    assert kwargs["cwd"] == str(launch_path.parent)
    assert kwargs["stdin"] is listary_search_module.subprocess.DEVNULL
    assert kwargs["stdout"] is listary_search_module.subprocess.DEVNULL
    assert kwargs["stderr"] is listary_search_module.subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert "shell" not in kwargs


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd.exe launcher contract")
def test_photo_launcher_dispatch_uses_exact_cmd_argv_with_startup_focus(tmp_path: Path, monkeypatch) -> None:
    launch_path = (tmp_path / "Launch Spiffy Photo Editor.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    cmd_path = (tmp_path / "cmd.exe").resolve()
    cmd_path.write_bytes(b"MZ")
    launched: list[tuple[list[str], dict[str, object]]] = []

    class _FakeProcess:
        pass

    def fake_popen(argv, **kwargs):
        launched.append(([str(part) for part in argv], dict(kwargs)))
        return _FakeProcess()

    monkeypatch.setenv("COMSPEC", str(cmd_path))
    monkeypatch.setattr(listary_search_module.subprocess, "Popen", fake_popen)
    assert listary_search_module._dispatch_photo_workspace_launcher(str(launch_path)) is True
    assert len(launched) == 1
    argv, kwargs = launched[0]
    assert argv == [
        str(cmd_path),
        "/d",
        "/c",
        str(launch_path),
        "--startup-focus-view",
        "photo",
    ]
    assert kwargs["cwd"] == str(launch_path.parent)
    assert "shell" not in kwargs


def test_photo_workspace_app_warm_receipt_records_launcher_activation(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    app = QApplication.instance() or QApplication([])
    launch_path = (tmp_path / "Launch Spiffy Photo Editor.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    synthetic = (
        tmp_path
        / "providers"
        / "workspace_catalog"
        / "ss-scripts"
        / "spiffy_workspace"
        / "spiffy_visual_clipboard.workspaceapp"
    )
    result = ListarySearchResult(
        item_id=305,
        path=str(synthetic),
        parent_path=str(synthetic.parent),
        name=synthetic.name,
        kind="app",
        preview_hint=_workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_visual_clipboard",
            label="Spiffy Clipboard",
            aliases=["Visual Clipboard", "Photo Editor"],
            launch_path=str(launch_path),
        ),
    )
    client = _RevalidatingQuickSearchClient(result)
    dialog = ListarySearchDialog(client=client)
    requests: list[dict[str, object]] = []
    dispatched: list[str] = []

    def completed_focus(request, **_kwargs):
        requests.append(dict(request))
        return {
            "schema": clipboard_ipc.IPC_SCHEMA_V2,
            "protocol_version": clipboard_ipc.IPC_PROTOCOL_VERSION,
            "action": "focus_view",
            "request_id": request["request_id"],
            "requested_creative_view": "photo",
            "accepted": True,
            "completed": True,
            "ok": True,
            "outcome": "focused_existing",
            "window_visible": True,
            "endpoint_contacted": True,
            "cold_launch_eligible": False,
        }

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", completed_focus)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_photo_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    try:
        dialog._search_finished(None, 0, listary_search_module.ListarySearchPage((result,), 0, 1), None)
        app.processEvents()
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert dispatched == []
        assert len(requests) == 1
        assert requests[0]["requested_creative_view"] == "photo"
        assert client.activation_sources == [(305, "launcher")]
        receipt = dialog._last_file_manager_receipt
        assert receipt is not None
        assert isinstance(receipt.destination, listary_search_module.WorkspaceApplicationLaunchReceipt)
        assert receipt.destination.delivery_mode == "warm_ipc"
        assert receipt.destination.requested_creative_view == "photo"
        assert "Verified Spiffy Clipboard" in dialog.status.text()
    finally:
        dialog.deleteLater()


def test_photo_workspace_app_cold_receipt_records_exactly_one_launcher_activation(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    app = QApplication.instance() or QApplication([])
    launch_path = (tmp_path / "Launch Spiffy Photo Editor.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    synthetic = (
        tmp_path
        / "providers"
        / "workspace_catalog"
        / "ss-scripts"
        / "spiffy_workspace"
        / "spiffy_visual_clipboard.workspaceapp"
    )
    result = ListarySearchResult(
        item_id=307,
        path=str(synthetic),
        parent_path=str(synthetic.parent),
        name=synthetic.name,
        kind="app",
        preview_hint=_workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_visual_clipboard",
            label="Spiffy Clipboard",
            aliases=["Visual Clipboard", "Photo Editor"],
            launch_path=str(launch_path),
        ),
    )
    client = _RevalidatingQuickSearchClient(result)
    dialog = ListarySearchDialog(client=client)
    requests: list[dict[str, object]] = []
    dispatched: list[str] = []

    def absent_then_completed(request, **_kwargs):
        requests.append(dict(request))
        if len(requests) == 1:
            return {
                "ok": False,
                "error": "editor_not_running",
                "endpoint_contacted": False,
                "cold_launch_eligible": True,
            }
        return {
            "schema": clipboard_ipc.IPC_SCHEMA_V2,
            "protocol_version": clipboard_ipc.IPC_PROTOCOL_VERSION,
            "action": "focus_view",
            "request_id": request["request_id"],
            "requested_creative_view": "photo",
            "accepted": True,
            "completed": True,
            "ok": True,
            "outcome": "focused_existing",
            "window_visible": True,
            "endpoint_contacted": True,
            "cold_launch_eligible": False,
        }

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", absent_then_completed)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_photo_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    try:
        dialog._search_finished(None, 0, listary_search_module.ListarySearchPage((result,), 0, 1), None)
        app.processEvents()
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert dispatched == [str(launch_path)]
        assert len({str(request["request_id"]) for request in requests}) == 1
        assert client.activation_sources == [(307, "launcher")]
        receipt = dialog._last_file_manager_receipt
        assert receipt is not None
        assert isinstance(receipt.destination, listary_search_module.WorkspaceApplicationLaunchReceipt)
        assert receipt.destination.delivery_mode == "cold_launcher"
        assert receipt.destination.dispatch_happened is True
    finally:
        dialog.deleteLater()


def test_photo_workspace_app_cold_launch_dispatches_once_and_reuses_request_id(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    launch_path = (tmp_path / "Launch Spiffy Photo Editor.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_visual_clipboard",
            label="Spiffy Clipboard",
            aliases=["Visual Clipboard", "Photo Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    requests: list[dict[str, object]] = []
    dispatched: list[str] = []

    def absent_then_completed(request, **_kwargs):
        requests.append(dict(request))
        if len(requests) == 1:
            return {
                "ok": False,
                "error": "editor_not_running",
                "endpoint_contacted": False,
                "cold_launch_eligible": True,
            }
        return {
            "schema": clipboard_ipc.IPC_SCHEMA_V2,
            "protocol_version": clipboard_ipc.IPC_PROTOCOL_VERSION,
            "action": "focus_view",
            "request_id": request["request_id"],
            "requested_creative_view": "photo",
            "accepted": True,
            "completed": True,
            "ok": True,
            "outcome": "focused_existing",
            "window_visible": True,
            "endpoint_contacted": True,
            "cold_launch_eligible": False,
        }

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", absent_then_completed)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_photo_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    receipt = listary_search_module._launch_photo_workspace_verified(
        metadata,
        cancel_event=threading.Event(),
        revalidate_target=lambda: None,
        timeout_seconds=1.0,
    )
    assert dispatched == [str(launch_path)]
    assert len(requests) == 2
    assert len({str(request["request_id"]) for request in requests}) == 1
    assert receipt.delivery_mode == "cold_launcher"
    assert receipt.dispatch_happened is True
    assert receipt.requested_creative_view == "photo"


def test_photo_workspace_owner_disappears_then_reconnects_without_dispatch(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    launch_path = (tmp_path / "Launch Spiffy Photo Editor.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_visual_clipboard",
            label="Spiffy Clipboard",
            aliases=["Photo Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    responses = [
        {
            "ok": False,
            "error": "ipc_request_processing",
            "endpoint_contacted": True,
            "cold_launch_eligible": False,
        },
        {
            "ok": False,
            "error": "editor_not_running",
            "endpoint_contacted": False,
            "cold_launch_eligible": True,
        },
    ]
    request_ids: list[str] = []
    dispatched: list[str] = []

    def reconnecting_owner(request, **_kwargs):
        request_ids.append(str(request["request_id"]))
        if responses:
            return responses.pop(0)
        return {
            "schema": clipboard_ipc.IPC_SCHEMA_V2,
            "protocol_version": clipboard_ipc.IPC_PROTOCOL_VERSION,
            "action": "focus_view",
            "request_id": request["request_id"],
            "requested_creative_view": "photo",
            "accepted": True,
            "completed": True,
            "ok": True,
            "outcome": "focused_existing",
            "window_visible": True,
            "endpoint_contacted": True,
            "cold_launch_eligible": False,
        }

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", reconnecting_owner)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_photo_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    receipt = listary_search_module._launch_photo_workspace_verified(
        metadata,
        cancel_event=threading.Event(),
        revalidate_target=lambda: None,
        timeout_seconds=1.0,
    )
    assert dispatched == []
    assert len(request_ids) == 3
    assert len(set(request_ids)) == 1
    assert receipt.delivery_mode == "warm_ipc"
    assert receipt.dispatch_happened is False


def test_photo_workspace_contacted_timeout_is_ambiguous_and_never_dispatches(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    launch_path = (tmp_path / "Launch Spiffy Photo Editor.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_visual_clipboard",
            label="Spiffy Clipboard",
            aliases=["Photo Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    request_ids: list[str] = []
    dispatched: list[str] = []

    def processing(request, **_kwargs):
        request_ids.append(str(request["request_id"]))
        return {
            "ok": False,
            "error": "ipc_ack_timeout",
            "endpoint_contacted": True,
            "cold_launch_eligible": False,
        }

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", processing)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_photo_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_photo_workspace_verified(
            metadata,
            cancel_event=threading.Event(),
            revalidate_target=lambda: None,
            timeout_seconds=0.25,
        )
    assert captured.value.code == "workspace_destination_ack_timeout"
    assert captured.value.ambiguous is True
    assert captured.value.dispatch_happened is False
    assert dispatched == []
    assert len(request_ids) > 1
    assert len(set(request_ids)) == 1


def test_photo_workspace_cancellation_before_dispatch_has_no_side_effect(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    launch_path = (tmp_path / "Launch Spiffy Photo Editor.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_visual_clipboard",
            label="Spiffy Clipboard",
            aliases=["Photo Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    cancel_event = threading.Event()
    cancel_event.set()
    sends: list[str] = []
    dispatched: list[str] = []
    monkeypatch.setattr(
        clipboard_ipc,
        "send_editor_request",
        lambda request, **_kwargs: sends.append(str(request["request_id"])) or {},
    )
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_photo_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_photo_workspace_verified(
            metadata,
            cancel_event=cancel_event,
            revalidate_target=lambda: None,
            timeout_seconds=1.0,
        )
    assert captured.value.code == "cancelled"
    assert captured.value.dispatch_happened is False
    assert sends == []
    assert dispatched == []


def test_photo_workspace_cancellation_after_dispatch_never_replays_launcher(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    launch_path = (tmp_path / "Launch Spiffy Photo Editor.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_visual_clipboard",
            label="Spiffy Clipboard",
            aliases=["Photo Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    cancel_event = threading.Event()
    sends: list[str] = []
    dispatched: list[str] = []

    def absent(request, **_kwargs):
        sends.append(str(request["request_id"]))
        return {
            "ok": False,
            "error": "editor_not_running",
            "endpoint_contacted": False,
            "cold_launch_eligible": True,
        }

    def dispatch_once(path):
        dispatched.append(str(path))
        cancel_event.set()
        return True

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", absent)
    monkeypatch.setattr(listary_search_module, "_dispatch_photo_workspace_launcher", dispatch_once)
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_photo_workspace_verified(
            metadata,
            cancel_event=cancel_event,
            revalidate_target=lambda: None,
            timeout_seconds=1.0,
        )
    assert captured.value.code == "cancelled"
    assert captured.value.ambiguous is True
    assert captured.value.dispatch_happened is True
    assert dispatched == [str(launch_path)]
    assert len(sends) == 1


def test_photo_workspace_timeout_after_dispatch_never_replays_launcher(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    launch_path = (tmp_path / "Launch Spiffy Photo Editor.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_visual_clipboard",
            label="Spiffy Clipboard",
            aliases=["Photo Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    request_ids: list[str] = []
    dispatched: list[str] = []

    def still_absent(request, **_kwargs):
        request_ids.append(str(request["request_id"]))
        return {
            "ok": False,
            "error": "editor_not_running",
            "endpoint_contacted": False,
            "cold_launch_eligible": True,
        }

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", still_absent)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_photo_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_photo_workspace_verified(
            metadata,
            cancel_event=threading.Event(),
            revalidate_target=lambda: None,
            timeout_seconds=0.25,
        )
    assert captured.value.code == "workspace_destination_ack_timeout"
    assert captured.value.ambiguous is True
    assert captured.value.dispatch_happened is True
    assert dispatched == [str(launch_path)]
    assert len(request_ids) > 1
    assert len(set(request_ids)) == 1


def test_photo_workspace_stale_after_dispatch_never_replays_launcher(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    launch_path = (tmp_path / "Launch Spiffy Photo Editor.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_visual_clipboard",
            label="Spiffy Clipboard",
            aliases=["Photo Editor"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    request_ids: list[str] = []
    dispatched: list[str] = []
    revalidation_count = 0

    def absent(request, **_kwargs):
        request_ids.append(str(request["request_id"]))
        return {
            "ok": False,
            "error": "editor_not_running",
            "endpoint_contacted": False,
            "cold_launch_eligible": True,
        }

    def revalidate():
        nonlocal revalidation_count
        revalidation_count += 1
        if revalidation_count >= 3:
            raise RuntimeError("workspace target changed")

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", absent)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_photo_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_photo_workspace_verified(
            metadata,
            cancel_event=threading.Event(),
            revalidate_target=revalidate,
            timeout_seconds=1.0,
        )
    assert captured.value.code == "stale_result"
    assert captured.value.ambiguous is True
    assert captured.value.dispatch_happened is True
    assert dispatched == [str(launch_path)]
    assert len(request_ids) == 1


def test_photo_workspace_outer_timeout_after_dispatch_gets_no_activation_credit(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    app = QApplication.instance() or QApplication([])
    launch_path = (tmp_path / "Launch Spiffy Photo Editor.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    synthetic = (
        tmp_path
        / "providers"
        / "workspace_catalog"
        / "ss-scripts"
        / "spiffy_workspace"
        / "spiffy_visual_clipboard.workspaceapp"
    )
    result = ListarySearchResult(
        item_id=308,
        path=str(synthetic),
        parent_path=str(synthetic.parent),
        name=synthetic.name,
        kind="app",
        preview_hint=_workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_visual_clipboard",
            label="Spiffy Clipboard",
            aliases=["Photo Editor"],
            launch_path=str(launch_path),
        ),
    )
    client = _RevalidatingQuickSearchClient(result)
    dialog = ListarySearchDialog(client=client)
    request_ids: list[str] = []
    dispatched: list[str] = []
    real_launch = listary_search_module._launch_photo_workspace_verified

    def still_absent(request, **_kwargs):
        request_ids.append(str(request["request_id"]))
        return {
            "ok": False,
            "error": "editor_not_running",
            "endpoint_contacted": False,
            "cold_launch_eligible": True,
        }

    def short_launch(metadata, *, cancel_event, revalidate_target, timeout_seconds=12.0):
        del timeout_seconds
        return real_launch(
            metadata,
            cancel_event=cancel_event,
            revalidate_target=revalidate_target,
            timeout_seconds=0.25,
        )

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", still_absent)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_photo_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    monkeypatch.setattr(listary_search_module, "_launch_photo_workspace_verified", short_launch)
    try:
        dialog._search_finished(None, 0, listary_search_module.ListarySearchPage((result,), 0, 1), None)
        app.processEvents()
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert dispatched == [str(launch_path)]
        assert len(request_ids) > 1
        assert len(set(request_ids)) == 1
        assert client.activation_sources == []
        assert dialog._last_file_manager_receipt is None
        failure = dialog._last_file_manager_failure_receipt
        assert failure is not None
        assert failure.code == "workspace_destination_ack_timeout"
        assert failure.ambiguous is True
        assert failure.dispatch_happened is True
        assert "Spiffy Clipboard state may have changed; no retry was attempted." in dialog.status.text()
    finally:
        dialog.deleteLater()


def test_photo_workspace_wrong_ack_gets_no_activation_credit(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    app = QApplication.instance() or QApplication([])
    launch_path = (tmp_path / "Launch Spiffy Photo Editor.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    synthetic = (
        tmp_path
        / "providers"
        / "workspace_catalog"
        / "ss-scripts"
        / "spiffy_workspace"
        / "spiffy_visual_clipboard.workspaceapp"
    )
    result = ListarySearchResult(
        item_id=306,
        path=str(synthetic),
        parent_path=str(synthetic.parent),
        name=synthetic.name,
        kind="app",
        preview_hint=_workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_visual_clipboard",
            label="Spiffy Clipboard",
            aliases=["Photo Editor"],
            launch_path=str(launch_path),
        ),
    )
    client = _RevalidatingQuickSearchClient(result)
    dialog = ListarySearchDialog(client=client)
    dispatched: list[str] = []

    monkeypatch.setattr(
        clipboard_ipc,
        "send_editor_request",
        lambda request, **_kwargs: {
            "ok": False,
            "error": "ipc_response_binding_failed",
            "detail": "response requested creative view mismatch",
            "request_id": "stale-request-id",
            "requested_creative_view": "storyboard",
            "endpoint_contacted": True,
            "cold_launch_eligible": False,
        },
    )
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_photo_workspace_launcher",
        lambda path: dispatched.append(str(path)) or True,
    )
    try:
        dialog._search_finished(None, 0, listary_search_module.ListarySearchPage((result,), 0, 1), None)
        app.processEvents()
        assert dialog._open_current() is True
        _wait_until(app, lambda: not dialog._file_manager_action_workers)
        assert QThreadPool.globalInstance().waitForDone(2_000) is True
        assert dispatched == []
        assert client.activation_sources == []
        assert dialog._last_file_manager_receipt is None
        assert dialog._last_file_manager_failure_receipt is not None
        assert dialog._last_file_manager_failure_receipt.code == "ipc_response_binding_failed"
    finally:
        dialog.deleteLater()


def test_storyboard_workspace_app_cold_launch_dispatches_canonical_launcher_once(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    launch_path = (tmp_path / "Launch Spiffy Storyboard.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_storyboard",
            label="Spiffy Storyboard",
            aliases=["storyboard"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    requests: list[dict[str, object]] = []
    opened: list[str] = []
    revalidations: list[int] = []

    def absent_then_completed(request, **_kwargs):
        requests.append(dict(request))
        if len(requests) == 1:
            return {
                "ok": False,
                "error": "editor_not_running",
                "endpoint_contacted": False,
                "cold_launch_eligible": True,
            }
        return {
            "schema": clipboard_ipc.IPC_SCHEMA_V2,
            "protocol_version": clipboard_ipc.IPC_PROTOCOL_VERSION,
            "action": "focus_view",
            "request_id": request["request_id"],
            "requested_creative_view": "storyboard",
            "accepted": True,
            "completed": True,
            "ok": True,
            "outcome": "focused_existing",
            "window_visible": True,
            "endpoint_contacted": True,
            "cold_launch_eligible": False,
        }

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", absent_then_completed)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_storyboard_workspace_launcher",
        lambda path: opened.append(str(path)) or True,
    )
    receipt = listary_search_module._launch_storyboard_workspace_verified(
        metadata,
        cancel_event=threading.Event(),
        revalidate_target=lambda: revalidations.append(len(revalidations)),
        timeout_seconds=1.0,
    )
    assert opened == [str(launch_path)]
    assert len(requests) == 2
    assert len({str(request["request_id"]) for request in requests}) == 1
    assert len(revalidations) == 3
    assert receipt.delivery_mode == "cold_launcher"
    assert receipt.dispatch_happened is True


def test_storyboard_workspace_app_reachable_ambiguous_owner_never_dispatches_launcher(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    launch_path = (tmp_path / "Launch Spiffy Storyboard.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_storyboard",
            label="Spiffy Storyboard",
            aliases=["storyboard"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    request_ids: list[str] = []
    opened: list[str] = []

    def reachable_timeout(request, **_kwargs):
        request_ids.append(str(request["request_id"]))
        return {
            "ok": False,
            "error": "ipc_ack_timeout",
            "endpoint_contacted": True,
            "cold_launch_eligible": False,
        }

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", reachable_timeout)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_storyboard_workspace_launcher",
        lambda path: opened.append(str(path)) or True,
    )
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_storyboard_workspace_verified(
            metadata,
            cancel_event=threading.Event(),
            revalidate_target=lambda: None,
            timeout_seconds=0.25,
        )
    assert captured.value.code == "workspace_destination_ack_timeout"
    assert captured.value.ambiguous is True
    assert captured.value.dispatch_happened is False
    assert opened == []
    assert len(request_ids) > 1
    assert len(set(request_ids)) == 1


def test_storyboard_workspace_app_cancel_after_dispatch_never_replays_launcher(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    launch_path = (tmp_path / "Launch Spiffy Storyboard.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_storyboard",
            label="Spiffy Storyboard",
            aliases=["storyboard"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    cancel_event = threading.Event()
    requests: list[str] = []
    opened: list[str] = []

    def absent(request, **_kwargs):
        requests.append(str(request["request_id"]))
        return {
            "ok": False,
            "error": "editor_not_running",
            "endpoint_contacted": False,
            "cold_launch_eligible": True,
        }

    def dispatch_once(path):
        opened.append(str(path))
        cancel_event.set()
        return True

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", absent)
    monkeypatch.setattr(listary_search_module, "_dispatch_storyboard_workspace_launcher", dispatch_once)
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_storyboard_workspace_verified(
            metadata,
            cancel_event=cancel_event,
            revalidate_target=lambda: None,
            timeout_seconds=1.0,
        )
    assert captured.value.code == "cancelled"
    assert captured.value.ambiguous is True
    assert captured.value.dispatch_happened is True
    assert opened == [str(launch_path)]
    assert len(requests) == 1


def test_storyboard_workspace_app_stale_after_dispatch_never_replays_launcher(tmp_path: Path, monkeypatch) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    launch_path = (tmp_path / "Launch Spiffy Storyboard.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_storyboard",
            label="Spiffy Storyboard",
            aliases=["storyboard"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    requests: list[str] = []
    opened: list[str] = []
    revalidation_count = 0

    def absent(request, **_kwargs):
        requests.append(str(request["request_id"]))
        return {
            "ok": False,
            "error": "editor_not_running",
            "endpoint_contacted": False,
            "cold_launch_eligible": True,
        }

    def revalidate():
        nonlocal revalidation_count
        revalidation_count += 1
        if revalidation_count >= 3:
            raise RuntimeError("workspace target changed")

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", absent)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_storyboard_workspace_launcher",
        lambda path: opened.append(str(path)) or True,
    )
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_storyboard_workspace_verified(
            metadata,
            cancel_event=threading.Event(),
            revalidate_target=revalidate,
            timeout_seconds=1.0,
        )
    assert captured.value.code == "stale_result"
    assert captured.value.ambiguous is True
    assert captured.value.dispatch_happened is True
    assert opened == [str(launch_path)]
    assert len(requests) == 1


def test_storyboard_workspace_app_timeout_after_dispatch_is_ambiguous_and_never_replays_launcher(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import workflow.image_clipboard_ipc as clipboard_ipc

    launch_path = (tmp_path / "Launch Spiffy Storyboard.bat").resolve()
    launch_path.write_text("@echo off\n", encoding="utf-8")
    metadata = listary_search_module.decode_workspace_application_preview_hint(
        _workspace_application_hint(
            tmp_path,
            launch_target_id="spiffy_storyboard",
            label="Spiffy Storyboard",
            aliases=["storyboard"],
            launch_path=str(launch_path),
        )
    )
    assert metadata is not None
    request_ids: list[str] = []
    opened: list[str] = []

    def still_absent(request, **_kwargs):
        request_ids.append(str(request["request_id"]))
        return {
            "ok": False,
            "error": "editor_not_running",
            "endpoint_contacted": False,
            "cold_launch_eligible": True,
        }

    monkeypatch.setattr(clipboard_ipc, "send_editor_request", still_absent)
    monkeypatch.setattr(
        listary_search_module,
        "_dispatch_storyboard_workspace_launcher",
        lambda path: opened.append(str(path)) or True,
    )
    with pytest.raises(listary_search_module.WorkspaceApplicationLaunchError) as captured:
        listary_search_module._launch_storyboard_workspace_verified(
            metadata,
            cancel_event=threading.Event(),
            revalidate_target=lambda: None,
            timeout_seconds=0.25,
        )
    assert captured.value.code == "workspace_destination_ack_timeout"
    assert captured.value.ambiguous is True
    assert captured.value.dispatch_happened is True
    assert opened == [str(launch_path)]
    assert len(request_ids) > 1
    assert len(set(request_ids)) == 1


def test_workspace_application_actions_follow_launcher_disappearance_and_reappearance(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    synthetic = tmp_path / "providers" / "workspace_catalog" / "ss-scripts" / "spiffy_workspace" / "visual_clipboard.workspaceapp"
    launch_path = (tmp_path / "Launch Visual Clipboard.bat").resolve()
    result = ListarySearchResult(
        item_id=303,
        path=str(synthetic),
        parent_path=str(synthetic.parent),
        name=synthetic.name,
        kind="app",
        preview_hint=_workspace_application_hint(tmp_path),
    )
    client = _ActionClient()
    dialog = ListarySearchDialog(client=client)
    try:
        _select_dialog_result(dialog, result)
        app.processEvents()
        assert dialog.open_button.isEnabled() is False
        assert dialog.reveal_button.isEnabled() is False
        assert dialog.copy_path_button.isEnabled() is False
        assert "Workspace application unavailable" in dialog.details.text()
        assert dialog._open_current() is False
        assert client.activations == []

        launch_path.write_text("@echo off\n", encoding="utf-8")
        dialog._sync_result_state()
        app.processEvents()
        assert dialog.open_button.isEnabled() is True
        assert dialog.reveal_button.isEnabled() is True
        assert dialog.copy_path_button.isEnabled() is True

        launch_path.unlink()
        dialog._sync_result_state()
        app.processEvents()
        assert dialog.open_button.isEnabled() is False
        assert dialog.reveal_button.isEnabled() is False
        assert dialog.copy_path_button.isEnabled() is False
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_malformed_workspace_application_metadata_disables_actions_without_synthetic_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    synthetic = tmp_path / "visual_clipboard.workspaceapp"
    result = ListarySearchResult(
        item_id=302,
        path=str(synthetic),
        parent_path=str(tmp_path),
        name=synthetic.name,
        kind="app",
        preview_hint="workspace_application_v1:{not-json",
    )
    client = _ActionClient()
    dialog = ListarySearchDialog(client=client)
    opened: list[str] = []
    revealed: list[str] = []
    monkeypatch.setattr(listary_search_module, "open_path", lambda path: opened.append(str(path)) or True)
    monkeypatch.setattr(listary_search_module, "reveal_in_file_manager", lambda path: revealed.append(str(path)) or True)
    try:
        _select_dialog_result(dialog, result)
        app.processEvents()
        assert dialog.open_button.isEnabled() is False
        assert dialog.reveal_button.isEnabled() is False
        assert dialog.copy_path_button.isEnabled() is False
        assert "Invalid workspace application metadata" in dialog.details.text()

        assert dialog._open_current() is False
        assert dialog._reveal_current() is False
        assert dialog._copy_current_path() is False
        assert opened == []
        assert revealed == []
        assert client.activations == []
        assert "metadata is invalid" in dialog.status.text()
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_refresh_policy_helpers_cover_all_modes_and_reject_invalid_values() -> None:
    assert listary_search_module.normalize_refresh_policy("automatic") == "automatic"
    assert listary_search_module.normalize_refresh_policy("never") == "never"
    assert listary_search_module.normalize_refresh_policy({"interval": {"seconds": 15}}) == {
        "interval": {"seconds": 15}
    }
    assert listary_search_module.normalize_refresh_policy({"daily": {"hour": 23, "minute": 59}}) == {
        "daily": {"hour": 23, "minute": 59}
    }
    assert listary_search_module.normalize_refresh_policy(
        {"weekly": {"weekday_from_monday": 6, "hour": 4, "minute": 5}}
    ) == {"weekly": {"weekday_from_monday": 6, "hour": 4, "minute": 5}}
    assert listary_search_module.refresh_policy_label({"interval": {"seconds": 90}}) == "Interval · 90s"
    assert listary_search_module.refresh_policy_label({"daily": {"hour": 6, "minute": 7}}) == "Daily · 06:07"
    assert listary_search_module.refresh_policy_label(
        {"weekly": {"weekday_from_monday": 0, "hour": 8, "minute": 30}}
    ) == "Weekly · Monday 08:30"

    invalid = (
        {"interval": {"seconds": 0}},
        {"daily": {"hour": 24, "minute": 0}},
        {"daily": {"hour": 2, "minute": 60}},
        {"weekly": {"weekday_from_monday": 7, "hour": 2, "minute": 0}},
        {"weekly": {"weekday_from_monday": 1, "hour": 24, "minute": 0}},
        {"unknown": {}},
    )
    for value in invalid:
        try:
            listary_search_module.normalize_refresh_policy(value)
        except ValueError:
            pass
        else:  # pragma: no cover - explicit failure message
            raise AssertionError(f"invalid refresh policy was accepted: {value!r}")


def test_root_policy_dialog_defaults_automatic_and_roundtrips_disabled_weekly(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    fresh = listary_search_module.ListaryRootPolicyDialog(path=str(tmp_path))
    existing_root = _root_record(tmp_path)
    existing_root["enabled"] = False
    existing_root["refresh_policy"] = {
        "weekly": {"weekday_from_monday": 3, "hour": 21, "minute": 15}
    }
    existing = listary_search_module.ListaryRootPolicyDialog(path=str(tmp_path), root=existing_root)
    try:
        assert fresh.configuration() == {"refresh_policy": "automatic", "enabled": True}
        assert fresh.interval_seconds.isEnabled() is False
        assert fresh.hour.isEnabled() is False

        assert existing.configuration() == {
            "refresh_policy": {
                "weekly": {"weekday_from_monday": 3, "hour": 21, "minute": 15}
            },
            "enabled": False,
        }
        assert existing.weekday.isEnabled() is True
        assert existing.hour.isEnabled() is True
        existing.policy.setCurrentIndex(existing.policy.findData("interval"))
        existing.interval_seconds.setValue(45)
        app.processEvents()
        assert existing.configuration()["refresh_policy"] == {"interval": {"seconds": 45}}
    finally:
        fresh.reject()
        existing.reject()
        fresh.deleteLater()
        existing.deleteLater()


def test_listary_agent_client_put_and_remove_root_use_canonical_rpc(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    token = "00000000000000000000000000000001"
    (state / "integration-endpoint-v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protocol_version": 1,
                "process_id": 123,
                "address": f"{host}:{port}",
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    root = _root_record(tmp_path / "root")
    requests: list[dict] = []
    accepts: list[int] = []

    def response(request: dict) -> dict:
        if request["method"] == "put_root":
            return {"id": request["id"], "ok": True, "data": request["params"], "error": None}
        return {"id": request["id"], "ok": True, "data": {"removed": True}, "error": None}

    server = _serve_frames(listener, 2, response, requests, accepts)
    client = ListaryAgentClient(state)
    try:
        updated = client.put_root(root)
        client.remove_root(root["id"])
    finally:
        client.close()
        server.join(timeout=2)
        listener.close()
    assert updated["id"] == root["id"]
    assert [request["method"] for request in requests] == ["put_root", "remove_root"]
    assert requests[0]["params"] == root
    assert requests[1]["params"] == {"root_id": root["id"]}
    assert accepts == [1]


class _RootManagementClient:
    def __init__(self, root: dict | None = None) -> None:
        self.roots: list[dict] = [dict(root)] if root is not None else []
        self.fail_list = False
        self.fail_put = False
        self.put_calls: list[dict] = []
        self.remove_calls: list[str] = []
        self.reconcile_calls: list[str] = []

    def list_roots(self):
        if self.fail_list:
            raise ListaryAgentError("unreachable", "fixture list failure")
        return tuple(dict(root) for root in self.roots)

    def add_root(self, path, *, kind="local"):
        root = _root_record(Path(path), root_id=f"11111111-1111-4111-8111-{len(self.roots) + 1:012d}")
        root["kind"] = kind
        self.roots.append(root)
        return dict(root)

    def put_root(self, root):
        if self.fail_put:
            raise ListaryAgentError("invalid_request", "fixture put failure")
        payload = dict(root)
        self.put_calls.append(payload)
        for index, current in enumerate(self.roots):
            if current["id"] == payload["id"]:
                self.roots[index] = payload
                return dict(payload)
        raise ListaryAgentError("root_not_found", "fixture root missing")

    def remove_root(self, root_id):
        self.remove_calls.append(str(root_id))
        self.roots = [root for root in self.roots if root["id"] != str(root_id)]

    def reconcile_root(self, root_id, *, request_id=None):
        del request_id
        self.reconcile_calls.append(str(root_id))
        return {"discovered": 1, "errors": 0}


def test_root_manager_add_edit_reload_failure_and_disabled_states(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    client = _RootManagementClient()
    manager = listary_search_module.ListaryRootManagementDialog(client=client)
    try:
        manager.reload()
        _drain_root_manager(app, manager)
        assert manager.table.rowCount() == 0
        assert manager.status.text() == "No indexed folders yet. Add a folder to enable file search."
        assert manager.add_button.isEnabled() is True
        assert manager.edit_button.isEnabled() is False

        manager._start_add_root(str(tmp_path / "indexed"), {"interval": {"seconds": 30}}, True)
        _drain_root_manager(app, manager)
        assert manager.table.rowCount() == 1
        assert client.roots[0]["refresh_policy"] == {"interval": {"seconds": 30}}
        assert client.reconcile_calls == [client.roots[0]["id"]]
        assert manager.table.item(0, 1).text() == "Interval · 30s"

        root = manager._selected_root()
        assert root is not None
        manager._start_edit_root(root, "never", False)
        _drain_root_manager(app, manager)
        assert client.roots[0]["refresh_policy"] == "never"
        assert client.roots[0]["enabled"] is False
        assert manager.table.item(0, 2).text() == "No"
        assert manager.reindex_button.isEnabled() is False
        manager.reload()
        _drain_root_manager(app, manager)
        assert manager.status.text() == "1 indexed folder, all disabled. Enable one to search local files."

        client.fail_put = True
        root = manager._selected_root()
        assert root is not None
        manager._start_edit_root(root, {"daily": {"hour": 5, "minute": 20}}, True)
        _drain_root_manager(app, manager)
        assert "Could not edit indexed folders" in manager.status.text()
        assert client.roots[0]["refresh_policy"] == "never"
        client.fail_put = False

        client.fail_list = True
        manager.reload()
        _drain_root_manager(app, manager)
        assert "Could not load indexed folders" in manager.status.text()
        assert manager.add_button.isEnabled() is False
        assert manager.edit_button.isEnabled() is False
        assert manager.refresh_button.isEnabled() is True
        client.fail_list = False
        manager.reload()
        _drain_root_manager(app, manager)
        assert manager.add_button.isEnabled() is True
        assert manager.table.rowCount() == 1
    finally:
        manager.shutdown()
        manager.deleteLater()


def test_root_manager_notifies_search_surfaces_only_after_root_readback(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    client = _RootManagementClient()
    observed: list[tuple[str, ...]] = []
    manager = listary_search_module.ListaryRootManagementDialog(
        client=client,
        roots_changed=lambda: observed.append(tuple(str(root["id"]) for root in client.list_roots())),
    )
    try:
        manager.reload()
        _drain_root_manager(app, manager)
        assert observed == []

        manager._start_add_root(str(tmp_path / "first"), "automatic", True)
        _drain_root_manager(app, manager)
        first_id = str(client.roots[0]["id"])
        assert observed == [(first_id,)]

        first = manager._selected_root()
        assert first is not None
        manager._start_add_root(str(tmp_path / "second"), "automatic", True)
        _drain_root_manager(app, manager)
        second_id = str(client.roots[1]["id"])
        assert observed[-1] == (first_id, second_id)

        manager.table.selectRow(1)
        second = manager._selected_root()
        assert second is not None
        manager._start_remove_root(second)
        _drain_root_manager(app, manager)
        assert client.remove_calls == [second_id]
        assert observed[-1] == (first_id,)
        assert manager.table.rowCount() == 1
    finally:
        manager.shutdown()
        manager.deleteLater()


def test_committed_root_change_retires_open_quick_and_deep_results_before_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    client = _RootManagementClient()
    dialog = ListarySearchDialog(client=client)
    deep_search = ListaryFileSearchDialog(dialog, client=client)
    dialog._file_search_dialog = deep_search
    result_path = tmp_path / "stale-root-result.txt"
    result = ListarySearchResult(
        item_id=8101,
        path=str(result_path),
        parent_path=str(tmp_path),
        name=result_path.name,
        kind="file",
        extension="txt",
        size_bytes=12,
        modified_unix_ms=1760000000000,
        available=True,
    )
    _select_dialog_result(dialog, result)
    deep_search.results.setRowCount(1)
    name_item = QTableWidgetItem(result.name)
    name_item.setData(deep_search.RESULT_ROLE, result)
    deep_search.results.setItem(0, 0, name_item)
    deep_search._select_result_row(0)
    assert dialog.results.count() == 1
    assert deep_search.results.rowCount() == 1
    assert dialog.open_button.isEnabled()
    assert deep_search.open_button.isEnabled()

    quick_generation = dialog._search_generation
    deep_generation = deep_search._generation
    quick_refreshes: list[int] = []
    deep_refreshes: list[int] = []
    monkeypatch.setattr(dialog, "_start_search", lambda: quick_refreshes.append(dialog._search_generation))
    monkeypatch.setattr(deep_search, "_start_search", lambda: deep_refreshes.append(deep_search._generation))
    manager = listary_search_module.ListaryRootManagementDialog(
        client=client,
        roots_changed=dialog._indexed_roots_changed,
    )
    try:
        manager.reload()
        _drain_root_manager(app, manager)
        manager._start_add_root(str(tmp_path / "new-root"), "automatic", True)
        _drain_root_manager(app, manager)

        assert dialog._search_generation > quick_generation
        assert deep_search._generation > deep_generation
        assert dialog.results.count() == 0
        assert deep_search.results.rowCount() == 0
        assert not dialog.open_button.isEnabled()
        assert not dialog.reveal_button.isEnabled()
        assert not dialog.copy_path_button.isEnabled()
        assert not deep_search.open_button.isEnabled()
        assert not deep_search.reveal_button.isEnabled()
        assert not deep_search.copy_path_button.isEnabled()
        assert quick_refreshes == [dialog._search_generation]
        assert deep_refreshes == [deep_search._generation]
        assert client.list_roots()[0]["path"] == str(tmp_path / "new-root")
    finally:
        manager.shutdown()
        manager.deleteLater()
        dialog._file_search_dialog = None
        deep_search.reject()
        deep_search.deleteLater()
        dialog.reject()
        dialog.deleteLater()

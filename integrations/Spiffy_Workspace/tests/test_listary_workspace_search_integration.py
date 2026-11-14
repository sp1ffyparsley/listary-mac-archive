from __future__ import annotations

from pathlib import Path
import threading
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from spiffy_ui import product_panels
from spiffy_ui.listary_search import (
    ListaryAgentError,
    ListarySearchPage,
    ListarySearchResult,
    ListaryWorkspaceSearchAdapter,
    listary_result_media_type,
)
from spiffy_ui.media_surface import MediaRoles, MediaSurfaceItem, UnifiedMediaBrowser
from spiffy_ui.editor_session import EditorSession
from spiffy_ui.panels import EffectsBrowserPanel, HomeView
from spiffy_ui.product_panels import LibraryMediaPanel
from workflow.image_clipboard_qt import SessionPickerDialog


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(app: QApplication, predicate, timeout_ms: int = 2_000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("condition did not become true before timeout")


def _result(
    item_id: int,
    path: Path,
    *,
    preview_hint: str = "",
    root_id: str = "",
    stable_file_id: str = "",
) -> ListarySearchResult:
    return ListarySearchResult(
        item_id=item_id,
        path=str(path),
        parent_path=str(path.parent),
        name=path.name,
        kind="file",
        extension=path.suffix.lstrip("."),
        preview_hint=preview_hint,
        root_id=root_id,
        stable_file_id=stable_file_id,
    )


class _FakeClient:
    def __init__(self, page: ListarySearchPage) -> None:
        self.page = page
        self.calls: list[dict] = []
        self.closed = False
        self.roots: list[dict] = []

    def search(self, query, *, kinds=(), categories=(), limit=40, request_id=None):
        self.calls.append(
            {
                "query": query,
                "kinds": tuple(kinds),
                "categories": tuple(categories),
                "limit": limit,
                "request_id": request_id,
            }
        )
        return self.page

    def cancel(self, request_id):
        self.calls.append({"cancel": request_id})

    def list_roots(self):
        self.calls.append({"list_roots": True})
        return tuple(dict(row) for row in self.roots)

    def add_root(self, path, *, kind="local"):
        root = {
            "id": f"root-{len(self.roots) + 1}",
            "path": str(Path(path).absolute()),
            "kind": kind,
            "availability": "available",
            "last_reconciled_unix_ms": None,
        }
        self.roots.append(root)
        self.calls.append({"add_root": root["path"], "kind": kind})
        return dict(root)

    def reconcile_root(self, root_id, *, request_id=None):
        for root in self.roots:
            if root["id"] == root_id:
                root["last_reconciled_unix_ms"] = 1
                root["availability"] = "available"
        self.calls.append({"reconcile_root": root_id, "request_id": request_id})
        return {"root_id": root_id}

    def close(self):
        self.closed = True


def test_workspace_adapter_filters_media_without_filesystem_probe(tmp_path: Path) -> None:
    page = ListarySearchPage(
        (
            _result(1, tmp_path / "clip.mp4", preview_hint="video"),
            _result(2, tmp_path / "poster.png"),
            _result(3, tmp_path / "notes.txt"),
        ),
        elapsed_micros=37,
        considered_candidates=11,
    )
    client = _FakeClient(page)
    adapter = ListaryWorkspaceSearchAdapter(client=client)

    filtered = adapter.search_files("poster", media_types=("image",), limit=4, request_id=91)

    assert [row.name for row in filtered.results] == ["poster.png"]
    assert filtered.elapsed_micros == 37
    assert client.calls == [
        {
            "query": "poster",
            "kinds": ("file",),
            "categories": ("picture",),
            "limit": 4,
            "request_id": 91,
        }
    ]
    assert listary_result_media_type(page.results[0]) == "video"
    assert listary_result_media_type(page.results[1]) == "image"
    assert listary_result_media_type(page.results[2]) == "other"
    adapter.close()
    assert client.closed is False


def test_workspace_adapter_admits_explicit_root_once_through_resident_owner(tmp_path: Path) -> None:
    source = tmp_path / "Clipboard Sessions"
    source.mkdir()
    client = _FakeClient(ListarySearchPage(()))
    adapter = ListaryWorkspaceSearchAdapter(client=client)

    adapter.search_files("poster", media_types=("image",), source_roots=(source,), request_id=501)
    adapter.search_files("poster again", media_types=("image",), source_roots=(source,), request_id=502)

    root_calls = [call for call in client.calls if "list_roots" in call or "add_root" in call or "reconcile_root" in call]
    assert root_calls == [
        {"list_roots": True},
        {"add_root": str(source.absolute()), "kind": "local"},
        {"reconcile_root": "root-1", "request_id": 501},
        {"list_roots": True},
    ]
    search_calls = [call for call in client.calls if "query" in call]
    assert [call["query"] for call in search_calls] == ["poster", "poster again"]


def test_workspace_adapter_respects_narrower_manual_root_and_missing_source(tmp_path: Path) -> None:
    requested = tmp_path / "Projects"
    narrower = requested / "Manual"
    narrower.mkdir(parents=True)
    client = _FakeClient(ListarySearchPage(()))
    client.roots.append(
        {
            "id": "manual-root",
            "path": str(narrower.absolute()),
            "kind": "local",
            "availability": "available",
            "last_reconciled_unix_ms": 1,
        }
    )
    adapter = ListaryWorkspaceSearchAdapter(client=client)

    overlap = adapter.ensure_indexed_root(requested)
    missing = adapter.ensure_indexed_root(tmp_path / "Missing")

    assert overlap.status == "overlap"
    assert overlap.root_id == "manual-root"
    assert missing.status == "missing"
    assert not any("add_root" in call or "reconcile_root" in call for call in client.calls)


def test_workspace_adapter_rechecks_cached_root_after_manual_removal(tmp_path: Path) -> None:
    source = tmp_path / "Library Media"
    source.mkdir()
    client = _FakeClient(ListarySearchPage(()))
    adapter = ListaryWorkspaceSearchAdapter(client=client)

    adapter.search_files("first", media_types=("video",), source_roots=(source,), request_id=601)
    client.roots.clear()
    adapter.search_files("second", media_types=("video",), source_roots=(source,), request_id=602)

    assert [call for call in client.calls if "add_root" in call] == [
        {"add_root": str(source.absolute()), "kind": "local"},
        {"add_root": str(source.absolute()), "kind": "local"},
    ]
    assert [call["request_id"] for call in client.calls if "reconcile_root" in call] == [601, 602]


def test_workspace_adapter_respects_disabled_covering_root_and_unsafe_auto_roots(tmp_path: Path) -> None:
    source = tmp_path / "Library Media"
    source.mkdir()
    client = _FakeClient(ListarySearchPage(()))
    client.roots.append(
        {
            "id": "disabled-root",
            "path": str(source.absolute()),
            "kind": "local",
            "enabled": False,
            "availability": "available",
            "last_reconciled_unix_ms": 1,
        }
    )
    adapter = ListaryWorkspaceSearchAdapter(client=client)

    disabled = adapter.ensure_indexed_root(source)
    home = adapter.ensure_indexed_root(Path.home())
    volume = adapter.ensure_indexed_root(Path(tmp_path.anchor))

    assert disabled.status == "disabled"
    assert home.status == "unsafe"
    assert volume.status == "unsafe"
    assert not any("add_root" in call or "reconcile_root" in call for call in client.calls)


def test_workspace_adapter_requires_explicit_listary_configuration_for_nonlocal_auto_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "Remote Media"
    source.mkdir()
    client = _FakeClient(ListarySearchPage(()))
    adapter = ListaryWorkspaceSearchAdapter(client=client)
    monkeypatch.setattr(adapter, "_root_kind_for_path", lambda _path: "network")

    admission = adapter.ensure_indexed_root(source)

    assert admission.status == "external"
    assert "explicit Listary root configuration" in admission.detail
    assert not any("add_root" in call or "reconcile_root" in call for call in client.calls)


def test_workspace_adapter_batches_root_listing_and_continues_after_one_root_error(tmp_path: Path) -> None:
    first = tmp_path / "First"
    second = tmp_path / "Second"
    first.mkdir()
    second.mkdir()
    client = _FakeClient(ListarySearchPage(()))
    original_reconcile = client.reconcile_root

    def reconcile(root_id, *, request_id=None):
        if root_id == "root-1":
            raise ListaryAgentError("io", "fixture root is inaccessible")
        return original_reconcile(root_id, request_id=request_id)

    client.reconcile_root = reconcile  # type: ignore[method-assign]
    adapter = ListaryWorkspaceSearchAdapter(client=client)

    admissions = adapter.ensure_indexed_roots((first, second), request_id=701)

    assert [row.status for row in admissions] == ["error", "indexed"]
    assert sum(1 for call in client.calls if call.get("list_roots")) == 1
    assert [call["add_root"] for call in client.calls if "add_root" in call] == [
        str(first.absolute()),
        str(second.absolute()),
    ]


def test_workspace_adapter_projects_use_canonical_folder_search_without_filesystem_probe(tmp_path: Path) -> None:
    project = tmp_path / "Forgotten Cut.spiffyedit"
    other_folder = tmp_path / "Forgotten References"
    unavailable = tmp_path / "Forgotten Offline.spiffyedit"
    page = ListarySearchPage(
        (
            ListarySearchResult(11, str(project), str(tmp_path), project.name, "folder"),
            ListarySearchResult(12, str(other_folder), str(tmp_path), other_folder.name, "folder"),
            ListarySearchResult(13, str(unavailable), str(tmp_path), unavailable.name, "folder", available=False),
            ListarySearchResult(14, str(tmp_path / "Fake.spiffyedit"), str(tmp_path), "Fake.spiffyedit", "file"),
        ),
        elapsed_micros=29,
        considered_candidates=17,
    )
    client = _FakeClient(page)
    adapter = ListaryWorkspaceSearchAdapter(client=client)

    result = adapter.search_projects("forgotten", limit=2, request_id=101)

    assert [row.path for row in result.results] == [str(project)]
    assert result.elapsed_micros == 29
    assert result.considered_candidates == 17
    assert client.calls == [
        {
            "query": "forgotten",
            "kinds": ("folder",),
            "categories": (),
            "limit": 8,
            "request_id": 101,
        }
    ]


def test_workspace_adapter_preset_files_use_canonical_file_search_without_filesystem_probe(tmp_path: Path) -> None:
    premiere = tmp_path / "Camera Shake.prfpset"
    after_effects = tmp_path / "Impact Shake.ffx"
    ordinary = tmp_path / "Impact Shake.txt"
    unavailable = tmp_path / "Offline Shake.ffx"
    page = ListarySearchPage(
        (
            _result(21, premiere),
            _result(22, after_effects),
            _result(23, ordinary),
            ListarySearchResult(
                24,
                str(unavailable),
                str(tmp_path),
                unavailable.name,
                "file",
                extension="ffx",
                available=False,
            ),
            ListarySearchResult(
                25,
                str(tmp_path / "Fake.prfpset"),
                str(tmp_path),
                "Fake.prfpset",
                "folder",
                extension="prfpset",
            ),
        ),
        elapsed_micros=31,
        considered_candidates=19,
    )
    client = _FakeClient(page)
    adapter = ListaryWorkspaceSearchAdapter(client=client)

    result = adapter.search_preset_files("shake", limit=2, request_id=111)

    assert [row.path for row in result.results] == [str(premiere), str(after_effects)]
    assert result.elapsed_micros == 31
    assert result.considered_candidates == 19
    assert client.calls == [
        {
            "query": "shake",
            "kinds": ("file",),
            "categories": (),
            "limit": 8,
            "request_id": 111,
        }
    ]


class _EffectsPresetAdapter:
    def __init__(
        self,
        rows_by_query: dict[str, tuple[ListarySearchResult, ...]],
        *,
        fail: bool = False,
        fail_revalidate: bool = False,
    ) -> None:
        self.rows_by_query = rows_by_query
        self.fail = fail
        self.fail_revalidate = fail_revalidate
        self.calls: list[str] = []
        self.revalidated: dict[int, ListarySearchResult] = {}
        self.revalidate_calls: list[int] = []

    def search_preset_files(self, query, *, limit=24, request_id=None):
        del limit, request_id
        query_text = str(query)
        self.calls.append(query_text)
        if self.fail:
            raise ListaryAgentError("not_running", "fixture indexed preset search unavailable")
        if query_text == "slow preset":
            time.sleep(0.12)
        return ListarySearchPage(tuple(self.rows_by_query.get(query_text, ())), elapsed_micros=16)

    def revalidate_result(self, displayed, *, refresh_label=""):
        del refresh_label
        self.revalidate_calls.append(int(displayed.item_id))
        if self.fail_revalidate:
            raise ListaryAgentError("stale_result", "fixture preset identity was replaced")
        return self.revalidated.get(int(displayed.item_id), displayed)


def _indexed_preset_items(panel: EffectsBrowserPanel) -> list:
    return [
        item
        for item in panel.tree.findItems(
            "*",
            Qt.MatchFlag.MatchWildcard | Qt.MatchFlag.MatchRecursive,
            0,
        )
        if str(item.data(0, panel.ENTRY_KIND_ROLE) or "") == "indexed_preset_file"
    ]


def test_effects_search_discovers_indexed_preset_files_as_import_only_candidates(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    premiere = tmp_path / "Camera Shake.prfpset"
    after_effects = tmp_path / "Impact Shake.ffx"
    adapter = _EffectsPresetAdapter({"shake": (_result(31, premiere), _result(32, after_effects))})
    session = EditorSession(tmp_path / "runtime", enable_audio=False)
    panel = EffectsBrowserPanel(session, listary_adapter=adapter)
    imported: list[tuple[str, str]] = []
    monkeypatch.setattr(
        panel,
        "import_premiere_preset_pack",
        lambda path: imported.append(("prfpset", str(path))) or True,
    )
    monkeypatch.setattr(
        panel,
        "import_after_effects_preset",
        lambda path, **_kwargs: imported.append(("ffx", str(path))) or True,
    )
    panel.show()
    try:
        panel.search.setText("shake")
        panel._indexed_preset_timer.stop()
        panel._start_indexed_preset_search()
        _wait_until(app, lambda: panel._indexed_preset_state == "ready")

        items = _indexed_preset_items(panel)
        assert [item.data(0, panel.INDEXED_PRESET_PATH_ROLE) for item in items] == [str(premiere), str(after_effects)]
        assert all(not item.data(0, Qt.ItemDataRole.UserRole) for item in items)
        assert all(not bool(item.flags() & Qt.ItemFlag.ItemIsDragEnabled) for item in items)

        panel.tree.setCurrentItem(items[0])
        app.processEvents()
        assert panel.apply_button.isEnabled() is False
        assert "Import this indexed preset file" in panel.apply_reason.text()
        menu = panel._build_context_menu(items[0])
        assert menu is not None
        assert [action.text() for action in menu.actions()] == ["Import Indexed Preset"]

        panel._activate(items[0], 0)
        assert imported == [("prfpset", str(premiere))]
        assert adapter.revalidate_calls == [31]
        assert panel.last_indexed_preset_action_receipt["outcome"] == "delivered"
        assert str(premiere) not in [
            str(item.data(0, panel.INDEXED_PRESET_PATH_ROLE) or "")
            for item in _indexed_preset_items(panel)
        ]
        remaining = _indexed_preset_items(panel)
        assert len(remaining) == 1
        panel._activate(remaining[0], 0)
        assert imported[-1] == ("ffx", str(after_effects))
        assert adapter.revalidate_calls[-1] == 32
    finally:
        assert panel.shutdown_background_work(1_000) is True
        panel.close()
        panel.deleteLater()
        app.processEvents()
        session.close()


def test_effects_indexed_preset_error_and_rapid_replacement_keep_native_effects_usable(
    app: QApplication,
    tmp_path: Path,
) -> None:
    session = EditorSession(tmp_path / "runtime-error", enable_audio=False)
    failing = EffectsBrowserPanel(session, listary_adapter=_EffectsPresetAdapter({}, fail=True))
    failing.show()
    try:
        failing.search.setText("blur")
        failing._indexed_preset_timer.stop()
        failing._start_indexed_preset_search()
        _wait_until(app, lambda: failing._indexed_preset_state == "error")
        native_uids = {
            str(item.data(0, Qt.ItemDataRole.UserRole) or "")
            for item in failing.tree.findItems("*", Qt.MatchFlag.MatchWildcard | Qt.MatchFlag.MatchRecursive, 0)
        }
        assert "spiffy.blur" in native_uids
        assert failing.indexed_preset_retry_button.isVisible() is True
        assert "built-in effects and installed presets still work" in failing.state_banner.text()
    finally:
        assert failing.shutdown_background_work(1_000) is True
        failing.close()
        failing.deleteLater()
        app.processEvents()
        session.close()

    slow = _result(41, tmp_path / "Slow Preset.prfpset")
    fast = _result(42, tmp_path / "Fast Preset.ffx")
    adapter = _EffectsPresetAdapter({"slow preset": (slow,), "fast preset": (fast,)})
    replacement_session = EditorSession(tmp_path / "runtime-replacement", enable_audio=False)
    replacement = EffectsBrowserPanel(replacement_session, listary_adapter=adapter)
    replacement.show()
    try:
        replacement.search.setText("slow preset")
        replacement._indexed_preset_timer.stop()
        replacement._start_indexed_preset_search()
        QTest.qWait(10)
        replacement.search.setText("fast preset")
        replacement._indexed_preset_timer.stop()
        replacement._start_indexed_preset_search()
        _wait_until(
            app,
            lambda: replacement._indexed_preset_state == "ready"
            and replacement._indexed_preset_pool.activeThreadCount() == 0,
        )
        items = _indexed_preset_items(replacement)
        assert len(items) == 1
        assert items[0].data(0, replacement.INDEXED_PRESET_PATH_ROLE) == str(fast.path)
    finally:
        assert replacement.shutdown_background_work(1_000) is True
        replacement.close()
        replacement.deleteLater()
        app.processEvents()
        replacement_session.close()


def test_effects_indexed_preset_action_follows_same_identity_move_and_rejects_replacement(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    displayed_path = tmp_path / "before" / "Currentness Shake.prfpset"
    moved_path = tmp_path / "after" / "Currentness Shake.prfpset"
    displayed = _result(
        61,
        displayed_path,
        root_id="root-effects",
        stable_file_id="stable-effects-61",
    )
    moved = _result(
        61,
        moved_path,
        root_id="root-effects",
        stable_file_id="stable-effects-61",
    )
    adapter = _EffectsPresetAdapter({"currentness shake": (displayed,)})
    adapter.revalidated[61] = moved
    session = EditorSession(tmp_path / "runtime-move", enable_audio=False)
    panel = EffectsBrowserPanel(session, listary_adapter=adapter)
    imported: list[str] = []
    monkeypatch.setattr(panel, "import_premiere_preset_pack", lambda path: imported.append(str(path)) or True)
    panel.show()
    try:
        panel.search.setText("currentness shake")
        panel._indexed_preset_timer.stop()
        panel._start_indexed_preset_search()
        _wait_until(app, lambda: panel._indexed_preset_state == "ready")
        item = _indexed_preset_items(panel)[0]
        panel._activate(item, 0)
        assert imported == [str(moved_path)]
        receipt = panel.last_indexed_preset_action_receipt
        assert receipt["outcome"] == "delivered"
        assert receipt["item_id"] == 61
        assert receipt["root_id"] == "root-effects"
        assert receipt["stable_file_id"] == "stable-effects-61"
        assert receipt["displayed_path"] == str(displayed_path)
        assert receipt["target_path"] == str(moved_path)
        assert receipt["moved"] is True
    finally:
        assert panel.shutdown_background_work(1_000) is True
        panel.close()
        panel.deleteLater()
        app.processEvents()
        session.close()

    rejecting = _EffectsPresetAdapter({"currentness shake": (displayed,)}, fail_revalidate=True)
    reject_session = EditorSession(tmp_path / "runtime-reject", enable_audio=False)
    rejected_panel = EffectsBrowserPanel(reject_session, listary_adapter=rejecting)
    rejected_imports: list[str] = []
    monkeypatch.setattr(
        rejected_panel,
        "import_premiere_preset_pack",
        lambda path: rejected_imports.append(str(path)) or True,
    )
    rejected_panel.show()
    try:
        rejected_panel.search.setText("currentness shake")
        rejected_panel._indexed_preset_timer.stop()
        rejected_panel._start_indexed_preset_search()
        _wait_until(app, lambda: rejected_panel._indexed_preset_state == "ready")
        rejected_panel._activate(_indexed_preset_items(rejected_panel)[0], 0)
        assert rejected_imports == []
        assert rejected_panel.last_indexed_preset_action_receipt["outcome"] == "rejected"
        assert rejected_panel.indexed_preset_retry_button.isVisible() is True
    finally:
        assert rejected_panel.shutdown_background_work(1_000) is True
        rejected_panel.close()
        rejected_panel.deleteLater()
        app.processEvents()
        reject_session.close()


class _HomeProjectAdapter:
    def __init__(self, rows_by_query: dict[str, tuple[ListarySearchResult, ...]], *, fail: bool = False) -> None:
        self.rows_by_query = rows_by_query
        self.fail = fail
        self.calls: list[str] = []
        self.source_roots: list[tuple[str, ...]] = []
        self.cancelled: list[int] = []
        self.revalidated: dict[int, ListarySearchResult] = {}
        self.revalidate_calls: list[int] = []
        self.fail_revalidate = False

    def search_projects(self, query, *, source_roots=(), limit=24, request_id=None):
        del limit, request_id
        query_text = str(query)
        self.calls.append(query_text)
        self.source_roots.append(tuple(str(path) for path in source_roots))
        if self.fail:
            raise ListaryAgentError("not_running", "fixture indexed project search unavailable")
        if query_text == "slow project":
            time.sleep(0.12)
        return ListarySearchPage(tuple(self.rows_by_query.get(query_text, ())), elapsed_micros=18)

    def cancel(self, request_id):
        self.cancelled.append(int(request_id))

    def revalidate_result(self, displayed, *, refresh_label=""):
        del refresh_label
        self.revalidate_calls.append(int(displayed.item_id))
        if self.fail_revalidate:
            raise ListaryAgentError("stale_result", "fixture project identity was replaced")
        return self.revalidated.get(int(displayed.item_id), displayed)


def _project_result(item_id: int, path: Path) -> ListarySearchResult:
    return ListarySearchResult(
        item_id=item_id,
        path=str(path),
        parent_path=str(path.parent),
        name=path.name,
        kind="folder",
    )


def test_home_project_search_merges_indexed_projects_dedupes_native_and_opens_through_existing_signal(
    app: QApplication,
    tmp_path: Path,
) -> None:
    recent = tmp_path / "Episode 3.spiffyedit"
    recent.mkdir()
    indexed = tmp_path / "Archive" / "Episode 3 Alt.spiffyedit"
    adapter = _HomeProjectAdapter(
        {"episode 3": (_project_result(201, recent), _project_result(202, indexed))}
    )
    home = HomeView(
        state_path=tmp_path / "runtime" / "settings" / "home.json",
        listary_adapter=adapter,
    )
    opened: list[ListarySearchResult] = []
    home.indexedProjectRequested.connect(opened.append)
    home.show()
    try:
        home.set_recent([str(recent)])
        home.search.setText("episode 3")
        home._indexed_project_timer.stop()
        home._start_indexed_project_search()
        _wait_until(app, lambda: home._indexed_project_state == "ready")

        assert home.recent.count() == 1
        assert home.indexed.count() == 1
        item = home.indexed.item(0)
        assert item.data(Qt.ItemDataRole.UserRole) == str(indexed)
        assert item.data(home.PROJECT_STATUS_ROLE) == "indexed"
        assert "Indexed by Listary" in item.toolTip()
        home._activate_indexed_project_item(item)
        assert len(opened) == 1
        assert opened[0].item_id == 202
        assert opened[0].path == str(indexed)
        assert adapter.source_roots == [(str(tmp_path.absolute()),)]
    finally:
        assert home.shutdown_background_work(1_000) is True
        home.close()
        home.deleteLater()
        app.processEvents()


def test_home_project_search_agent_error_and_rapid_replacement_keep_native_home_usable(
    app: QApplication,
    tmp_path: Path,
) -> None:
    recent = tmp_path / "Native Project.spiffyedit"
    recent.mkdir()
    failing = _HomeProjectAdapter({}, fail=True)
    home = HomeView(
        state_path=tmp_path / "runtime" / "settings" / "home.json",
        listary_adapter=failing,
    )
    home.show()
    try:
        home.set_recent([str(recent)])
        home.search.setText("native project")
        home._indexed_project_timer.stop()
        home._start_indexed_project_search()
        _wait_until(app, lambda: home._indexed_project_state == "error")
        assert home.recent.count() == 1
        assert home.indexed_retry.isVisible() is True
        assert "recent and pinned projects still work" in home.indexed_status.text()
    finally:
        assert home.shutdown_background_work(1_000) is True
        home.close()
        home.deleteLater()
        app.processEvents()

    slow = tmp_path / "Slow Project.spiffyedit"
    fast = tmp_path / "Fast Project.spiffyedit"
    adapter = _HomeProjectAdapter(
        {
            "slow project": (_project_result(301, slow),),
            "fast project": (_project_result(302, fast),),
        }
    )
    replacement = HomeView(
        state_path=tmp_path / "runtime-2" / "settings" / "home.json",
        listary_adapter=adapter,
    )
    replacement.show()
    try:
        replacement.search.setText("slow project")
        replacement._indexed_project_timer.stop()
        replacement._start_indexed_project_search()
        QTest.qWait(10)
        replacement.search.setText("fast project")
        replacement._indexed_project_timer.stop()
        replacement._start_indexed_project_search()
        _wait_until(
            app,
            lambda: replacement._indexed_project_state == "ready"
            and replacement._indexed_project_pool.activeThreadCount() == 0,
        )
        assert replacement.indexed.count() == 1
        assert replacement.indexed.item(0).data(Qt.ItemDataRole.UserRole) == str(fast)
        _wait_until(app, lambda: bool(adapter.cancelled))
    finally:
        assert replacement.shutdown_background_work(1_000) is True
        replacement.close()
        replacement.deleteLater()
        app.processEvents()


class _ClipboardAdapter:
    def __init__(
        self,
        rows_by_query: dict[str, tuple[ListarySearchResult, ...]],
        *,
        fail: bool = False,
        fail_code: str = "not_running",
        fail_message: str = "fixture agent unavailable",
        fail_revalidate: bool = False,
    ) -> None:
        self.rows_by_query = rows_by_query
        self.fail = fail
        self.fail_code = str(fail_code)
        self.fail_message = str(fail_message)
        self.fail_revalidate = fail_revalidate
        self.calls: list[str] = []
        self.source_roots: list[tuple[str, ...]] = []
        self.cancelled: list[int] = []
        self.revalidated: dict[int, ListarySearchResult] = {}
        self.revalidate_calls: list[int] = []

    def search_files(self, query, *, media_types=(), source_roots=(), limit=40, request_id=None):
        del limit, request_id
        self.calls.append(str(query))
        self.source_roots.append(tuple(str(path) for path in source_roots))
        assert tuple(media_types) == ("image",)
        if self.fail:
            raise ListaryAgentError(self.fail_code, self.fail_message)
        if str(query) == "slow first":
            time.sleep(0.12)
        return ListarySearchPage(tuple(self.rows_by_query.get(str(query), ())), elapsed_micros=21)

    def cancel(self, request_id):
        self.cancelled.append(int(request_id))

    def revalidate_result(self, displayed, *, refresh_label=""):
        del refresh_label
        self.revalidate_calls.append(int(displayed.item_id))
        if self.fail_revalidate:
            raise ListaryAgentError("stale_result", "fixture indexed image was replaced")
        return self.revalidated.get(int(displayed.item_id), displayed)


def test_clipboard_home_merges_indexed_images_and_opens_selected_file(
    app: QApplication,
    tmp_path: Path,
) -> None:
    indexed = tmp_path / "episode-poster.png"
    adapter = _ClipboardAdapter({"episode poster": (_result(101, indexed),)})
    dialog = SessionPickerDialog(root=tmp_path / "sessions", listary_adapter=adapter)
    dialog.show()
    try:
        dialog.session_search.setText("episode poster")
        dialog._listary_timer.stop()
        dialog._start_listary_search()
        _wait_until(app, lambda: dialog._listary_state == "ready")

        texts = [dialog.session_list.item(i).text() for i in range(dialog.session_list.count())]
        assert "Indexed Images · Listary" in texts
        indexed_item = next(item for i in range(dialog.session_list.count()) if (item := dialog.session_list.item(i)).data(Qt.ItemDataRole.UserRole) == str(indexed))
        assert "Indexed Image · Listary" in indexed_item.text()
        dialog.session_list.setCurrentItem(indexed_item)
        app.processEvents()
        assert dialog.preview_details.text().startswith("Listary · Indexed Image")
        assert dialog.picker_status.text() == "0 recent | 1 indexed"

        dialog._open_selected()
        assert dialog.action == "open_image"
        assert dialog.source_path == str(indexed)
        assert dialog.selected_path == ""
        assert adapter.source_roots == [(str((tmp_path / "sessions").absolute()),)]
        assert adapter.revalidate_calls == [101]
        assert dialog.listary_open_receipt["outcome"] == "verified"
        assert dialog.listary_open_receipt["item_id"] == 101
        assert dialog.listary_open_receipt["target_path"] == str(indexed)
        assert dialog.listary_open_receipt["moved"] is False
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_clipboard_home_revalidates_same_identity_move_and_keeps_keyboard_focus(
    app: QApplication,
    tmp_path: Path,
) -> None:
    original = tmp_path / "sessions" / "before" / "episode-poster.png"
    moved = tmp_path / "sessions" / "after" / "episode-poster-renamed.png"
    displayed = _result(
        501,
        original,
        root_id="root-photo",
        stable_file_id="stable-photo-501",
    )
    authoritative = _result(
        501,
        moved,
        root_id="root-photo",
        stable_file_id="stable-photo-501",
    )
    adapter = _ClipboardAdapter({"episode poster": (displayed,)})
    adapter.revalidated[501] = authoritative
    dialog = SessionPickerDialog(root=tmp_path / "sessions", listary_adapter=adapter)
    dialog.show()
    try:
        dialog.session_search.setText("episode poster")
        dialog._listary_timer.stop()
        dialog._start_listary_search()
        _wait_until(app, lambda: dialog._listary_state == "ready")

        indexed_item = next(
            item
            for i in range(dialog.session_list.count())
            if (item := dialog.session_list.item(i)).data(Qt.ItemDataRole.UserRole) == str(original)
        )
        dialog.session_list.setCurrentItem(indexed_item)
        dialog.session_search.setFocus()
        QTest.keyClick(dialog.session_search, Qt.Key.Key_Down)
        app.processEvents()
        assert dialog.session_search.hasFocus() is True
        assert dialog.session_list.currentItem() is indexed_item

        QTest.keyClick(dialog.session_search, Qt.Key.Key_Return)
        app.processEvents()
        assert dialog.action == "open_image"
        assert dialog.source_path == str(moved)
        assert adapter.revalidate_calls == [501]
        receipt = dialog.listary_open_receipt
        assert receipt["outcome"] == "verified"
        assert receipt["item_id"] == 501
        assert receipt["root_id"] == "root-photo"
        assert receipt["stable_file_id"] == "stable-photo-501"
        assert receipt["displayed_path"] == str(original)
        assert receipt["target_path"] == str(moved)
        assert receipt["moved"] is True
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_clipboard_home_rejects_replaced_indexed_image_before_open(
    app: QApplication,
    tmp_path: Path,
) -> None:
    indexed = tmp_path / "sessions" / "episode-poster.png"
    adapter = _ClipboardAdapter(
        {
            "episode poster": (
                _result(
                    601,
                    indexed,
                    root_id="root-photo",
                    stable_file_id="stable-photo-601",
                ),
            )
        },
        fail_revalidate=True,
    )
    dialog = SessionPickerDialog(root=tmp_path / "sessions", listary_adapter=adapter)
    dialog.show()
    try:
        dialog.session_search.setText("episode poster")
        dialog._listary_timer.stop()
        dialog._start_listary_search()
        _wait_until(app, lambda: dialog._listary_state == "ready")
        indexed_item = next(
            item
            for i in range(dialog.session_list.count())
            if (item := dialog.session_list.item(i)).data(Qt.ItemDataRole.UserRole) == str(indexed)
        )
        dialog.session_list.setCurrentItem(indexed_item)
        dialog._open_selected()
        app.processEvents()

        assert dialog.action == ""
        assert dialog.source_path == ""
        assert dialog.isVisible() is True
        assert dialog.indexed_retry.isVisible() is True
        assert "changed or unavailable" in dialog.picker_status.text()
        receipt = dialog.listary_open_receipt
        assert receipt["outcome"] == "rejected"
        assert receipt["item_id"] == 601
        assert receipt["target_path"] == ""
        assert "replaced" in receipt["detail"]
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_clipboard_home_agent_error_falls_back_and_retry_is_visible(
    app: QApplication,
    tmp_path: Path,
) -> None:
    adapter = _ClipboardAdapter({}, fail=True)
    dialog = SessionPickerDialog(root=tmp_path / "sessions", listary_adapter=adapter)
    dialog.show()
    try:
        dialog.session_search.setText("missing poster")
        dialog._listary_timer.stop()
        dialog._start_listary_search()
        _wait_until(app, lambda: dialog._listary_state == "error")
        assert dialog.indexed_retry.isVisible() is True
        assert "Indexed images unavailable" in dialog.picker_status.text()
        assert dialog.open_selected_button.isEnabled() is False
        assert "Indexed search unavailable" in dialog.preview_label.text()
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_clipboard_home_permission_error_fails_open_without_indexed_action(
    app: QApplication,
    tmp_path: Path,
) -> None:
    adapter = _ClipboardAdapter(
        {},
        fail=True,
        fail_code="permission_denied",
        fail_message="fixture indexed folder permission denied",
    )
    dialog = SessionPickerDialog(root=tmp_path / "sessions", listary_adapter=adapter)
    dialog.show()
    try:
        dialog.session_search.setText("protected poster")
        dialog._listary_timer.stop()
        dialog._start_listary_search()
        _wait_until(app, lambda: dialog._listary_state == "error")
        assert dialog.indexed_retry.isVisible() is True
        assert dialog.action == ""
        assert dialog.source_path == ""
        assert dialog.open_selected_button.isEnabled() is False
        assert "permission denied" in dialog.picker_status.text()
        assert "Indexed folder permission denied" in dialog.preview_label.text()
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_clipboard_home_rapid_query_replacement_discards_slow_result(
    app: QApplication,
    tmp_path: Path,
) -> None:
    slow = _result(201, tmp_path / "slow-first.png")
    fast = _result(202, tmp_path / "fast-second.png")
    adapter = _ClipboardAdapter({"slow first": (slow,), "fast second": (fast,)})
    dialog = SessionPickerDialog(root=tmp_path / "sessions", listary_adapter=adapter)
    dialog.show()
    try:
        dialog.session_search.setText("slow first")
        dialog._listary_timer.stop()
        dialog._start_listary_search()
        QTest.qWait(10)
        dialog.session_search.setText("fast second")
        dialog._listary_timer.stop()
        dialog._start_listary_search()
        _wait_until(app, lambda: dialog._listary_state == "ready" and dialog._listary_pool.activeThreadCount() == 0)
        visible_paths = {
            str(dialog.session_list.item(i).data(Qt.ItemDataRole.UserRole) or "")
            for i in range(dialog.session_list.count())
        }
        assert str(fast.path) in visible_paths
        assert str(slow.path) not in visible_paths
        _wait_until(app, lambda: bool(adapter.cancelled))
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


class _VideoAdapter:
    def __init__(self, page: ListarySearchPage, *, fail: bool = False) -> None:
        self.page = page
        self.fail = fail
        self.calls: list[str] = []
        self.source_roots: list[tuple[str, ...]] = []
        self.request_ids: list[int | None] = []
        self.cancelled: list[int] = []

    def search_files(self, query, *, media_types=(), source_roots=(), limit=40, request_id=None):
        del limit
        self.calls.append(str(query))
        self.source_roots.append(tuple(str(path) for path in source_roots))
        self.request_ids.append(request_id)
        if self.fail:
            raise ListaryAgentError("not_running", "fixture indexed search unavailable")
        allowed = set(media_types)
        return ListarySearchPage(
            tuple(row for row in self.page.results if listary_result_media_type(row) in allowed),
            elapsed_micros=self.page.elapsed_micros,
        )

    def cancel(self, request_id):
        self.cancelled.append(int(request_id))


class _BlockingVideoAdapter:
    def __init__(self, fast_result: ListarySearchResult) -> None:
        self.fast_result = fast_result
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[str, int | None]] = []
        self.cancelled: list[int] = []

    def search_files(self, query, *, media_types=(), source_roots=(), limit=40, request_id=None):
        del media_types, source_roots, limit
        self.calls.append((str(query), request_id))
        if str(query) == "slow library":
            self.started.set()
            self.release.wait(2.0)
            raise ListaryAgentError("cancelled", "fixture stale Library request cancelled")
        return ListarySearchPage((self.fast_result,), elapsed_micros=12)

    def cancel(self, request_id):
        self.cancelled.append(int(request_id))
        self.release.set()


def test_video_library_keeps_native_owner_dedupes_path_and_preserves_fuzzy_listary_match(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = tmp_path / "native-and-indexed.mp4"
    fuzzy = tmp_path / "neon_transition_master.mp4"
    duplicate_name_a = tmp_path / "a" / "duplicate_asset.mp4"
    duplicate_name_b = tmp_path / "b" / "duplicate_asset.mp4"
    native = MediaSurfaceItem(
        owner="Spiffy Library",
        domain="media",
        item_id="native",
        title=duplicate.name,
        path=str(duplicate),
        media_type="video",
    )
    monkeypatch.setattr(product_panels, "load_library_items", lambda *_args, **_kwargs: [native])
    library_root = tmp_path / "library-root"
    library_root.mkdir()
    monkeypatch.setattr(product_panels, "load_library_search_roots", lambda: (str(library_root),))
    adapter = _VideoAdapter(
        ListarySearchPage(
            (
                _result(301, duplicate, preview_hint="video"),
                _result(302, fuzzy, preview_hint="video"),
                _result(303, duplicate_name_a, preview_hint="video"),
                _result(304, duplicate_name_b, preview_hint="video"),
            ),
            elapsed_micros=44,
        )
    )
    panel = LibraryMediaPanel(auto_refresh=False, listary_adapter=adapter)
    try:
        rows = panel._load_scoped_items("neon trnsition")
        assert rows[0].owner == "Spiffy Library"
        assert sum(1 for row in rows if row.path == str(duplicate)) == 1
        indexed = [row for row in rows if row.owner == "Listary"]
        assert [row.path for row in indexed] == [str(fuzzy), str(duplicate_name_a), str(duplicate_name_b)]
        assert all(row.subtitle.startswith("Listary · ") for row in indexed)
        assert all(row.metadata["search_query_authoritative"] == "neon trnsition" for row in indexed)
        assert adapter.source_roots == [(str(library_root),)]
        assert adapter.request_ids[0]

        panel.browser.set_items(rows)
        panel.browser.search.setText("neon trnsition")
        app.processEvents()
        visible_paths = {
            str(panel.browser.proxy.index(row, 0).data(MediaRoles.PATH) or "")
            for row in range(panel.browser.proxy.rowCount())
        }
        assert str(fuzzy) in visible_paths
        assert panel.browser.proxy.rowCount() >= 1
        panel._sync_listary_status()
        assert panel.indexed_status.text() == "Listary indexed files · 3 additional matches · 0.04 ms"
    finally:
        panel.shutdown_background_work(1_000)
        panel.deleteLater()
        app.processEvents()


def test_video_library_agent_error_keeps_native_results_and_exposes_refresh_retry(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = MediaSurfaceItem(
        owner="Spiffy Library",
        domain="media",
        item_id="native",
        title="Native Clip",
        path=str(tmp_path / "native.mp4"),
        media_type="video",
    )
    monkeypatch.setattr(product_panels, "load_library_items", lambda *_args, **_kwargs: [native])
    monkeypatch.setattr(product_panels, "load_library_search_roots", lambda: ())
    panel = LibraryMediaPanel(
        auto_refresh=False,
        listary_adapter=_VideoAdapter(ListarySearchPage(()), fail=True),
    )
    try:
        rows = panel._load_scoped_items("native")
        assert rows == [native]
        panel.browser.set_items(rows)
        panel._sync_listary_status()
        assert "native Library results shown" in panel.indexed_status.text()
        assert "Refresh to retry" in panel.indexed_status.text()
    finally:
        panel.shutdown_background_work(1_000)
        panel.deleteLater()
        app.processEvents()


def test_video_library_replacement_query_cancels_running_listary_admission(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast = _result(401, tmp_path / "fast-library.mp4", preview_hint="video")
    adapter = _BlockingVideoAdapter(fast)
    monkeypatch.setattr(product_panels, "load_library_items", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(product_panels, "load_library_search_roots", lambda: ())
    panel = LibraryMediaPanel(auto_refresh=False, listary_adapter=adapter)
    panel.show()
    panel.set_active(True)
    try:
        panel.browser.search.setText("slow library")
        panel._refresh_immediately()
        _wait_until(app, adapter.started.is_set)
        panel.browser.search.setText("fast library")
        panel._refresh_immediately()
        _wait_until(
            app,
            lambda: panel._listary_status[0] == "ready"
            and panel.browser._pool.activeThreadCount() == 0,
        )
        _wait_until(
            app,
            lambda: any(row.path == str(fast.path) for row in panel.browser.model.items()),
        )
        assert adapter.cancelled
        assert [query for query, _request_id in adapter.calls] == ["slow library", "fast library"]
        assert any(row.path == str(fast.path) for row in panel.browser.model.items())
    finally:
        assert panel.shutdown_background_work(1_000) is True
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_unified_media_search_arrow_keys_move_selection_without_leaving_query(
    app: QApplication,
) -> None:
    browser = UnifiedMediaBrowser()
    browser.set_items(
        [
            MediaSurfaceItem(owner="owner", domain="media", item_id="a", title="Alpha"),
            MediaSurfaceItem(owner="owner", domain="media", item_id="b", title="Beta"),
        ]
    )
    browser.show()
    try:
        browser.search.setFocus()
        QTest.keyClick(browser.search, Qt.Key.Key_Down)
        app.processEvents()
        assert browser.view.currentIndex().row() == 0
        QTest.keyClick(browser.search, Qt.Key.Key_Down)
        app.processEvents()
        assert browser.view.currentIndex().row() == 1
        QTest.keyClick(browser.search, Qt.Key.Key_Up)
        app.processEvents()
        assert browser.view.currentIndex().row() == 0
        assert browser.search.hasFocus() is True
    finally:
        browser.shutdown_background_work(1_000)
        browser.close()
        browser.deleteLater()
        app.processEvents()

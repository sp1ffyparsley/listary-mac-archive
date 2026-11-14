from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import replace
import hashlib
from pathlib import Path
import os
import threading
import time
from types import SimpleNamespace

import pytest

from spiffy_ui import listary_windows_file_dialog as dialog_module
from spiffy_ui.listary_windows_file_dialog import WindowsFileDialogAdapter, WindowsFileDialogError


@pytest.mark.parametrize("foreground_pid", [0, -1, os.getpid() + 100])
def test_native_focus_handoff_never_steals_an_unowned_foreground(
    foreground_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not raise an old Save/Open modal over another user's active application."""
    touched: list[str] = []
    backend = object.__new__(dialog_module._WindowsUiAutomationBackend)
    backend.foreground_window = lambda: 98765
    backend.win32process = SimpleNamespace(
        GetWindowThreadProcessId=lambda hwnd: (44, foreground_pid)
    )
    backend.win32gui = SimpleNamespace(
        ShowWindow=lambda *_args: touched.append("show"),
        BringWindowToTop=lambda *_args: touched.append("bring"),
        SetForegroundWindow=lambda *_args: touched.append("focus"),
    )
    backend.win32con = SimpleNamespace(SW_RESTORE=9)
    monkeypatch.setattr(dialog_module.ctypes, "windll", SimpleNamespace(
        kernel32=SimpleNamespace(GetCurrentThreadId=lambda: 88),
        user32=SimpleNamespace(AttachThreadInput=lambda *_args: True),
    ))
    assert backend.make_foreground(101, timeout_seconds=0.05) is False
    assert touched == []


def test_native_focus_handoff_keeps_already_active_target_unmodified() -> None:
    backend = object.__new__(dialog_module._WindowsUiAutomationBackend)
    backend.foreground_window = lambda: 101
    assert backend.make_foreground(101, timeout_seconds=0.05) is True


def test_native_focus_handoff_allows_task_owned_qt_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    focused = [98765]
    events: list[str] = []
    backend = object.__new__(dialog_module._WindowsUiAutomationBackend)
    backend.foreground_window = lambda: focused[0]
    backend.win32process = SimpleNamespace(
        GetWindowThreadProcessId=lambda hwnd: (44 if hwnd == 98765 else 55, os.getpid())
    )
    backend.win32gui = SimpleNamespace(
        ShowWindow=lambda *_args: events.append("show"),
        BringWindowToTop=lambda *_args: events.append("bring"),
        SetForegroundWindow=lambda hwnd: (events.append("focus"), focused.__setitem__(0, hwnd)),
    )
    backend.win32con = SimpleNamespace(SW_RESTORE=9)
    monkeypatch.setattr(dialog_module.ctypes, "windll", SimpleNamespace(
        kernel32=SimpleNamespace(GetCurrentThreadId=lambda: 88),
        user32=SimpleNamespace(AttachThreadInput=lambda *_args: True),
    ))
    assert backend.make_foreground(101, timeout_seconds=0.05) is True
    assert events == ["show", "bring", "focus"]
    assert focused == [101]


@pytest.mark.parametrize("dialog_kind", ["open", "save"])
def test_native_accept_invoke_exception_after_possible_dispatch_is_ambiguous(dialog_kind: str) -> None:
    """An automation transport failure after Invoke starts may have delivered the action."""

    descriptor = _descriptor(dialog_kind=dialog_kind, current_directory="C:/fixture")
    context = dialog_module.WindowsFileDialogContext(
        schema="listary_windows_file_context_v1",
        context_token="task-owned-invoke-fault",
        generation=1,
        hwnd=descriptor.hwnd,
        process_id=descriptor.process_id,
        thread_id=descriptor.thread_id,
        owner_hwnd=descriptor.owner_hwnd,
        runtime_id=descriptor.runtime_id,
        window_class=descriptor.window_class,
        title=descriptor.title,
        dialog_kind=dialog_kind,
        current_directory=descriptor.current_directory,
        process_start_time_ns=descriptor.process_start_time_ns,
    )
    invoked: list[str] = []

    class _Invoke:
        def Invoke(self) -> None:
            invoked.append(dialog_kind)
            raise OSError("COM reply lost after native action may have been dispatched")

    class _Button:
        NativeWindowHandle = 501
        ControlTypeName = "ButtonControl"
        IsEnabled = True

        def GetRuntimeId(self):
            return (42, 501)

        def GetInvokePattern(self):
            return _Invoke()

    backend = object.__new__(dialog_module._WindowsUiAutomationBackend)
    backend.win32gui = SimpleNamespace(
        GetDlgItem=lambda hwnd, control_id: 501,
        GetClassName=lambda hwnd: "Button",
        GetWindowText=lambda hwnd: "&Open" if dialog_kind == "open" else "&Save",
    )
    backend.automation = SimpleNamespace(ControlFromHandle=lambda hwnd: _Button())
    backend._require_context_current = lambda _context: None
    backend._require_control_in_context = lambda *_args, **_kwargs: (42, 501)
    backend.make_foreground = lambda hwnd, timeout_seconds: True
    backend.foreground_window = lambda: context.hwnd
    backend.read_file_name = lambda _context: "proof.txt"

    with pytest.raises(WindowsFileDialogError) as error:
        if dialog_kind == "open":
            backend.accept_open_result(context, deadline=time.monotonic() + 2.0, cancelled=None)
        else:
            backend.accept_save_result(
                context,
                target_path="C:/fixture/proof.txt",
                filename="proof.txt",
                deadline=time.monotonic() + 2.0,
                cancelled=None,
            )
    assert invoked == [dialog_kind]
    assert error.value.code == "accept_invoke_failed"
    assert error.value.ambiguous is True
    assert error.value.acceptance_may_have_occurred is True


def _descriptor(
    *,
    hwnd: int = 101,
    process_id: int = 202,
    thread_id: int = 303,
    owner_hwnd: int = 404,
    runtime_id: tuple[int, ...] = (42, 7),
    dialog_kind: str = "open",
    current_directory: str | None = None,
    process_start_time_ns: int = 123456789,
) -> dialog_module._DialogDescriptor:
    return dialog_module._DialogDescriptor(
        hwnd=hwnd,
        process_id=process_id,
        thread_id=thread_id,
        owner_hwnd=owner_hwnd,
        runtime_id=runtime_id,
        window_class="#32770",
        title="Open" if dialog_kind == "open" else "Save As",
        dialog_kind=dialog_kind,
        current_directory=current_directory,
        process_start_time_ns=process_start_time_ns,
    )


class _RecordedContext(AbstractContextManager[None]):
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> None:
        self.events.append("enter")
        return None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback
        self.events.append("exit")
        return None


class _FakeBackend:
    def __init__(self, descriptor: dialog_module._DialogDescriptor | None) -> None:
        self.descriptor = descriptor
        self.foreground = descriptor.hwnd if descriptor is not None else 0
        self.context_events: list[str] = []
        self.navigation_calls: list[str] = []
        self.file_name_calls: list[str] = []
        self.selection_calls: list[str] = []
        self.accept_calls = 0
        self.accept_error: WindowsFileDialogError | None = None
        self.accept_delay_seconds = 0.0
        self.cancel_on_accept: threading.Event | None = None
        self.save_accept_calls = 0
        self.save_accept_error: WindowsFileDialogError | None = None
        self.save_accept_delay_seconds = 0.0
        self.save_overwrite_confirmed = True
        self.save_bytes = b"saved fixture"
        self.save_write_artifact = True
        self.navigation_error: WindowsFileDialogError | None = None
        self.navigation_readback_override: str | None = None
        self.file_name_readback_override: str | None = None
        self.initial_file_name = ""
        self.selection_readback_override: str | None = None
        self.current_context = True
        self.change_directory_on_file_name: str | None = None
        self.change_directory_on_file_selection: str | None = None
        self.file_name_delay_seconds = 0.0
        self.selection_delay_seconds = 0.0
        self.navigation_entered = threading.Event()
        self.navigation_release: threading.Event | None = None
        self.foreground_after_describe: int | None = None

    def automation_context(self) -> _RecordedContext:
        return _RecordedContext(self.context_events)

    def foreground_window(self) -> int:
        return self.foreground

    def make_foreground(self, hwnd: int, *, timeout_seconds: float) -> bool:
        del timeout_seconds
        if self.descriptor is None or int(hwnd) != self.descriptor.hwnd:
            return False
        self.foreground = int(hwnd)
        return True

    def describe(self, hwnd: int) -> dialog_module._DialogDescriptor | None:
        if self.descriptor is None or int(hwnd) != self.descriptor.hwnd:
            return None
        descriptor = self.descriptor
        if self.foreground_after_describe is not None:
            self.foreground = self.foreground_after_describe
        return descriptor

    def context_is_current(self, context) -> bool:
        del context
        return self.current_context

    def commit_directory_navigation(
        self,
        context,
        target: str,
        *,
        deadline: float,
        cancelled: threading.Event | None,
    ) -> tuple[str | None, bool]:
        del deadline
        assert self.descriptor is not None
        assert context.hwnd == self.descriptor.hwnd
        assert cancelled is None or not cancelled.is_set()
        self.navigation_calls.append(str(target))
        self.navigation_entered.set()
        if self.navigation_release is not None:
            assert self.navigation_release.wait(2.0)
        if self.navigation_error is not None:
            raise self.navigation_error
        readback = self.navigation_readback_override or str(target)
        self.descriptor = replace(self.descriptor, current_directory=readback)
        return readback, True

    def set_file_name(self, context, filename: str) -> str:
        assert self.descriptor is not None
        assert context.hwnd == self.descriptor.hwnd
        self.file_name_calls.append(str(filename))
        if self.file_name_delay_seconds:
            time.sleep(self.file_name_delay_seconds)
        if self.change_directory_on_file_name is not None:
            self.descriptor = replace(
                self.descriptor,
                current_directory=self.change_directory_on_file_name,
            )
        return self.file_name_readback_override or str(filename)

    def read_file_name(self, context) -> str:
        assert self.descriptor is not None
        assert context.hwnd == self.descriptor.hwnd
        if self.file_name_readback_override is not None:
            return self.file_name_readback_override
        return self.file_name_calls[-1] if self.file_name_calls else self.initial_file_name

    def select_file_item(
        self,
        context,
        target_path: str,
        filename: str,
        *,
        deadline: float,
        cancelled: threading.Event | None,
    ) -> str:
        del deadline, target_path
        assert self.descriptor is not None
        assert context.hwnd == self.descriptor.hwnd
        assert cancelled is None or not cancelled.is_set()
        self.selection_calls.append(str(filename))
        if self.selection_delay_seconds:
            time.sleep(self.selection_delay_seconds)
        if self.change_directory_on_file_selection is not None:
            self.descriptor = replace(
                self.descriptor,
                current_directory=self.change_directory_on_file_selection,
            )
        return self.selection_readback_override or str(filename)

    def accept_open_result(
        self,
        context,
        *,
        deadline: float,
        cancelled: threading.Event | None,
    ) -> None:
        del deadline
        assert self.descriptor is not None
        assert context.hwnd == self.descriptor.hwnd
        assert context.dialog_kind == "open"
        assert cancelled is None or not cancelled.is_set()
        self.accept_calls += 1
        if self.cancel_on_accept is not None:
            self.cancel_on_accept.set()
        if self.accept_delay_seconds:
            time.sleep(self.accept_delay_seconds)
        if self.accept_error is not None:
            raise self.accept_error
        self.descriptor = None
        self.foreground = context.owner_hwnd

    def accept_save_result(
        self,
        context,
        *,
        target_path: str,
        filename: str,
        deadline: float,
        cancelled: threading.Event | None,
    ) -> bool:
        del deadline
        assert self.descriptor is not None
        assert context.hwnd == self.descriptor.hwnd
        assert context.dialog_kind == "save"
        assert cancelled is None or not cancelled.is_set()
        assert Path(target_path).name == filename
        self.save_accept_calls += 1
        if self.save_accept_delay_seconds:
            time.sleep(self.save_accept_delay_seconds)
        if self.save_accept_error is not None:
            raise self.save_accept_error
        if self.save_write_artifact:
            Path(target_path).write_bytes(self.save_bytes)
        self.descriptor = None
        self.foreground = context.owner_hwnd
        return self.save_overwrite_confirmed


def _apply(adapter: WindowsFileDialogAdapter, context, target: Path, *, kind: str):
    return adapter.apply_result(
        context,
        target_path=str(target),
        item_kind=kind,
        item_id=77,
        root_id="root-a",
        stable_file_id="stable-a",
        displayed_path=str(target),
        query_text="scene",
        activation_generation=9,
    )


def test_capture_rejects_unrecognized_window() -> None:
    backend = _FakeBackend(None)
    adapter = WindowsFileDialogAdapter(backend=backend)

    assert adapter.capture_foreground() is None
    assert backend.context_events == ["enter", "exit"]


def test_capture_generation_is_stable_for_same_dialog_and_advances_for_replacement(tmp_path: Path) -> None:
    backend = _FakeBackend(_descriptor(current_directory=str(tmp_path)))
    adapter = WindowsFileDialogAdapter(backend=backend)

    first = adapter.capture_foreground()
    second = adapter.capture_foreground()
    assert first is not None and second is not None
    assert first.context_token == second.context_token
    assert first.generation == second.generation == 1

    backend.descriptor = replace(backend.descriptor, runtime_id=(42, 8))
    third = adapter.capture_foreground()
    assert third is not None
    assert third.context_token != first.context_token
    assert third.generation == 2


def test_prepare_context_refocuses_same_verified_dialog_without_mutation(tmp_path: Path) -> None:
    backend = _FakeBackend(_descriptor(current_directory=str(tmp_path)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    backend.foreground = 999

    adapter.prepare_context_for_action(context)

    assert backend.foreground == context.hwnd
    assert backend.navigation_calls == []
    assert backend.selection_calls == []
    assert backend.file_name_calls == []


def test_process_start_identity_drift_rejects_reused_dialog_identity(tmp_path: Path) -> None:
    backend = _FakeBackend(_descriptor(current_directory=str(tmp_path), process_start_time_ns=111))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    target = tmp_path / "target"
    target.mkdir()

    backend.descriptor = replace(backend.descriptor, process_start_time_ns=222)

    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, context, target, kind="folder")

    assert captured.value.code == "stale_context"
    assert backend.navigation_calls == []


def test_older_context_stays_stale_even_if_original_dialog_identity_reappears(tmp_path: Path) -> None:
    first_descriptor = _descriptor(current_directory=str(tmp_path), runtime_id=(42, 1))
    backend = _FakeBackend(first_descriptor)
    adapter = WindowsFileDialogAdapter(backend=backend)
    first = adapter.capture_foreground()
    assert first is not None

    backend.descriptor = replace(first_descriptor, hwnd=102, runtime_id=(42, 2))
    backend.foreground = 102
    second = adapter.capture_foreground()
    assert second is not None and second.generation == first.generation + 1

    backend.descriptor = first_descriptor
    backend.foreground = first_descriptor.hwnd
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, first, target, kind="folder")

    assert captured.value.code == "stale_context"
    assert backend.navigation_calls == []


def test_failed_foreground_capture_invalidates_previous_context(tmp_path: Path) -> None:
    backend = _FakeBackend(_descriptor(current_directory=str(tmp_path)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    backend.foreground = 0
    assert adapter.capture_foreground() is None
    backend.foreground = context.hwnd
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, context, target, kind="folder")

    assert captured.value.code == "stale_context"
    assert backend.navigation_calls == []


def test_capture_rejects_foreground_change_during_identity_probe(tmp_path: Path) -> None:
    backend = _FakeBackend(_descriptor(current_directory=str(tmp_path)))
    backend.foreground_after_describe = 999
    adapter = WindowsFileDialogAdapter(backend=backend)

    assert adapter.capture_foreground() is None


def test_owner_modal_change_fails_before_navigation(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    backend = _FakeBackend(_descriptor(current_directory=str(tmp_path)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    backend.current_context = False

    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, context, target, kind="folder")

    assert captured.value.code == "stale_context"
    assert backend.navigation_calls == []


def test_capture_rejects_dialog_that_is_not_owner_current_modal(tmp_path: Path) -> None:
    backend = _FakeBackend(_descriptor(current_directory=str(tmp_path)))
    backend.current_context = False
    adapter = WindowsFileDialogAdapter(backend=backend)

    assert adapter.capture_foreground() is None


def test_folder_navigation_requires_semantic_readback_and_never_accepts_dialog(tmp_path: Path) -> None:
    start = tmp_path / "start"
    target = tmp_path / "Résumé Folder Ω"
    start.mkdir()
    target.mkdir()
    backend = _FakeBackend(_descriptor(current_directory=str(start)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    receipt = _apply(adapter, context, target, kind="folder")

    assert backend.navigation_calls == [str(target)]
    assert backend.file_name_calls == []
    assert receipt.outcome == "verified"
    assert receipt.action == "navigate"
    assert receipt.directory_before == str(start)
    assert receipt.directory_after == str(target)
    assert receipt.selected_path_after is None
    assert receipt.accepted_by_dialog is False
    assert receipt.ambiguous is False
    assert backend.context_events == ["enter", "exit", "enter", "exit"]


@pytest.mark.parametrize("dialog_kind", ["open", "save"])
def test_exact_file_action_uses_semantic_open_selection_or_save_preparation(tmp_path: Path, dialog_kind: str) -> None:
    start = tmp_path / "start"
    target_dir = tmp_path / "Target Folder Ω"
    start.mkdir()
    target_dir.mkdir()
    target = target_dir / "shot résumé 01.mov"
    target.write_bytes(b"fixture")
    backend = _FakeBackend(_descriptor(dialog_kind=dialog_kind, current_directory=str(start)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    receipt = _apply(adapter, context, target, kind="file")

    assert backend.navigation_calls == [str(target_dir)]
    assert receipt.dialog_kind == dialog_kind
    assert receipt.item_kind == "file"
    assert receipt.accepted_by_dialog is False
    assert receipt.acceptance_may_have_occurred is False
    if dialog_kind == "open":
        assert backend.selection_calls == [target.name]
        assert backend.file_name_calls == []
        assert receipt.action == "select"
        assert Path(receipt.selected_path_after or "") == target
        assert receipt.prepared_filename_after is None
    else:
        assert backend.selection_calls == []
        assert backend.file_name_calls == [target.name]
        assert receipt.action == "prepare"
        assert receipt.selected_path_after is None
        assert receipt.prepared_filename_after == target.name


def test_native_save_filename_uses_exact_focused_keyboard_input_before_readback() -> None:
    """The shell's visual ValuePattern alone is not accepted by real Notepad Save As."""

    class _Pattern:
        def __init__(self, control) -> None:
            self.control = control

        @property
        def Value(self):
            return self.control.value

        def SetValue(self, _value) -> None:
            raise AssertionError("Save filename must use exact focused input, not ValuePattern.SetValue")

    class _Control:
        NativeWindowHandle = 501
        AutomationId = str(dialog_module._SAVE_FILE_NAME_CONTROL_ID)
        ControlTypeName = "EditControl"
        Name = dialog_module._FILE_NAME_LABEL

        def __init__(self) -> None:
            self.value = "source.txt"
            self.keys: list[str] = []

        def GetRuntimeId(self):
            return (42, 501)

        def SetFocus(self) -> None:
            automation.focused = self

        def SendKeys(self, text: str, **_kwargs) -> None:
            self.keys.append(text)
            if text == "{Ctrl}a{Delete}":
                self.value = ""
            else:
                self.value = text

        def GetValuePattern(self):
            return _Pattern(self)

    class _Automation:
        def __init__(self) -> None:
            self.focused = None

        def ControlFromHandle(self, hwnd: int):
            assert hwnd == 501
            return control

        def GetFocusedControl(self):
            return self.focused

    class _Win32Gui:
        @staticmethod
        def GetClassName(hwnd: int) -> str:
            assert hwnd == 501
            return "Edit"

        @staticmethod
        def GetDlgCtrlID(hwnd: int) -> int:
            assert hwnd == 501
            return dialog_module._SAVE_FILE_NAME_CONTROL_ID

        @staticmethod
        def GetAncestor(hwnd: int, _kind: int) -> int:
            assert hwnd == 501
            return 101

    class _Win32Process:
        @staticmethod
        def GetWindowThreadProcessId(hwnd: int):
            assert hwnd == 501
            return (303, 202)

    control = _Control()
    automation = _Automation()
    backend = object.__new__(dialog_module._WindowsUiAutomationBackend)
    backend.automation = automation
    backend.win32gui = _Win32Gui()
    backend.win32process = _Win32Process()
    backend.win32con = type("_Win32Con", (), {"GA_ROOT": 2})()
    backend._find_descendant = lambda _hwnd, **_kwargs: 501
    backend._require_context_current = lambda _context: _descriptor(
        dialog_kind="save",
        current_directory="C:/fixture",
    )
    backend._require_control_in_context = lambda _context, got, **_kwargs: got.GetRuntimeId()
    context = dialog_module.WindowsFileDialogContext(
        schema="listary_windows_file_context_v1",
        context_token="windows-dialog:fixture",
        generation=1,
        hwnd=101,
        process_id=202,
        thread_id=303,
        owner_hwnd=404,
        runtime_id=(42, 7),
        window_class="#32770",
        title="Save As",
        dialog_kind="save",
        current_directory="C:/fixture",
        process_start_time_ns=123456789,
    )

    value = backend.set_file_name(context, "fresh-name.txt")

    assert value == "fresh-name.txt"
    assert control.keys == ["{Ctrl}a{Delete}", "fresh-name.txt"]


def test_save_filename_keyboard_literal_escapes_braces_without_changing_unicode() -> None:
    assert dialog_module._send_keys_literal("take {résumé Ω}.txt") == "take {{}résumé Ω{}}.txt"


def test_stale_dialog_identity_fails_before_navigation(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    backend = _FakeBackend(_descriptor(current_directory=str(tmp_path)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    backend.descriptor = replace(backend.descriptor, process_id=999)

    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, context, target, kind="folder")

    assert captured.value.code == "stale_context"
    assert captured.value.ambiguous is False
    assert backend.navigation_calls == []


def test_cancel_before_native_change_fails_without_navigation(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    backend = _FakeBackend(_descriptor(current_directory=str(tmp_path)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(WindowsFileDialogError) as captured:
        adapter.apply_result(
            context,
            target_path=str(target),
            item_kind="folder",
            item_id=1,
            root_id="root-a",
            stable_file_id="stable-a",
            displayed_path=str(target),
            query_text="target",
            activation_generation=1,
            cancel_event=cancelled,
        )

    assert captured.value.code == "cancelled"
    assert backend.navigation_calls == []
    assert backend.context_events[-2:] == ["enter", "exit"]


def test_navigation_readback_mismatch_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    wrong = tmp_path / "wrong"
    target.mkdir()
    wrong.mkdir()
    backend = _FakeBackend(_descriptor(current_directory=str(tmp_path)))
    backend.navigation_readback_override = str(wrong)
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, context, target, kind="folder")

    assert captured.value.code == "readback_mismatch"
    assert backend.navigation_calls == [str(target)]


def test_file_name_readback_mismatch_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="save", current_directory=str(tmp_path)))
    backend.file_name_readback_override = "other.mov"
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, context, target, kind="file")

    assert captured.value.code == "readback_mismatch"
    assert backend.file_name_calls == [target.name]


def test_open_semantic_selection_readback_mismatch_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="open", current_directory=str(tmp_path)))
    backend.selection_readback_override = "other.mov"
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, context, target, kind="file")

    assert captured.value.code == "readback_mismatch"
    assert backend.selection_calls == [target.name]
    assert backend.file_name_calls == []


def test_open_selection_rechecks_live_folder_after_semantic_selection(tmp_path: Path) -> None:
    target_dir = tmp_path / "target"
    other_dir = tmp_path / "other"
    target_dir.mkdir()
    other_dir.mkdir()
    target = target_dir / "scene.mov"
    target.write_bytes(b"fixture")
    backend = _FakeBackend(_descriptor(current_directory=str(target_dir)))
    backend.change_directory_on_file_selection = str(other_dir)
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, context, target, kind="file")

    assert captured.value.code == "readback_mismatch"
    assert backend.selection_calls == [target.name]


def test_file_target_revalidator_runs_before_and_after_semantic_file_action(tmp_path: Path) -> None:
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target = target_dir / "scene.mov"
    target.write_bytes(b"fixture")
    backend = _FakeBackend(_descriptor(current_directory=str(target_dir)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    checks: list[int] = []

    receipt = adapter.apply_result(
        context,
        target_path=str(target),
        item_kind="file",
        item_id=77,
        root_id="root-a",
        stable_file_id="stable-a",
        displayed_path=str(target),
        query_text="scene",
        activation_generation=9,
        revalidate_target=lambda: checks.append(1),
    )

    assert receipt.outcome == "verified"
    assert checks == [1, 1]
    assert backend.selection_calls == [target.name]


def test_timeout_after_filename_write_is_retained_as_ambiguous_and_never_retried(tmp_path: Path) -> None:
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="save", current_directory=str(tmp_path)))
    backend.file_name_delay_seconds = 0.3
    adapter = WindowsFileDialogAdapter(backend=backend, default_timeout_seconds=0.25)
    context = adapter.capture_foreground()
    assert context is not None

    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, context, target, kind="file")

    assert captured.value.code == "selection_timeout_after_write"
    assert captured.value.ambiguous is True
    assert captured.value.receipt is not None
    assert captured.value.receipt.ambiguous is True
    assert backend.file_name_calls == [target.name]


def test_success_receipt_carries_action_result_generations_and_nonacceptance(tmp_path: Path) -> None:
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="open", current_directory=str(tmp_path)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    receipt = adapter.apply_result(
        context,
        target_path=str(target),
        item_kind="file",
        item_id=77,
        root_id="root-a",
        stable_file_id="stable-a",
        displayed_path=str(target),
        query_text="scene",
        activation_generation=9,
        action_generation=12,
        result_generation=9,
    )

    assert receipt.action_generation == 12
    assert receipt.result_generation == 9
    assert receipt.activation_generation == 9
    assert receipt.item_kind == "file"
    assert receipt.accepted_by_dialog is False
    assert receipt.acceptance_may_have_occurred is False


def test_open_file_commit_invokes_exact_dialog_acceptance_once(tmp_path: Path) -> None:
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="open", current_directory=str(tmp_path)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    checks: list[int] = []

    receipt = adapter.apply_result(
        context,
        target_path=str(target),
        item_kind="file",
        item_id=77,
        root_id="root-a",
        stable_file_id="stable-a",
        displayed_path=str(target),
        query_text="scene",
        activation_generation=9,
        action_generation=12,
        result_generation=9,
        revalidate_target=lambda: checks.append(1),
        accept_open=True,
    )

    assert checks == [1, 1, 1]
    assert backend.selection_calls == [target.name]
    assert backend.accept_calls == 1
    assert receipt.action == "open"
    assert receipt.accepted_by_dialog is True
    assert receipt.acceptance_may_have_occurred is False
    assert receipt.selected_path_after == str(target)
    assert "Open was invoked" in receipt.detail


def test_open_file_cancel_before_acceptance_never_invokes_open(tmp_path: Path) -> None:
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="open", current_directory=str(tmp_path)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    cancelled = threading.Event()
    checks = 0

    def revalidate() -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            cancelled.set()

    with pytest.raises(WindowsFileDialogError) as captured:
        adapter.apply_result(
            context,
            target_path=str(target),
            item_kind="file",
            item_id=77,
            root_id="root-a",
            stable_file_id="stable-a",
            displayed_path=str(target),
            query_text="scene",
            activation_generation=9,
            revalidate_target=revalidate,
            cancel_event=cancelled,
            accept_open=True,
        )

    assert captured.value.code == "cancelled"
    assert captured.value.ambiguous is False
    assert captured.value.acceptance_may_have_occurred is False
    assert backend.accept_calls == 0


def test_open_file_acceptance_ambiguity_is_never_retried(tmp_path: Path) -> None:
    target = tmp_path / "scene.mov"
    target.write_bytes(b"fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="open", current_directory=str(tmp_path)))
    backend.accept_error = WindowsFileDialogError(
        "acceptance_timeout",
        "fixture timeout after semantic Open invocation",
        ambiguous=True,
        acceptance_may_have_occurred=True,
    )
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    with pytest.raises(WindowsFileDialogError) as captured:
        adapter.apply_result(
            context,
            target_path=str(target),
            item_kind="file",
            item_id=77,
            root_id="root-a",
            stable_file_id="stable-a",
            displayed_path=str(target),
            query_text="scene",
            activation_generation=9,
            accept_open=True,
        )

    assert captured.value.code == "acceptance_timeout"
    assert captured.value.ambiguous is True
    assert captured.value.acceptance_may_have_occurred is True
    assert captured.value.receipt is not None
    assert captured.value.receipt.action == "open"
    assert captured.value.receipt.acceptance_may_have_occurred is True
    assert backend.accept_calls == 1


def test_save_file_commit_invokes_exact_dialog_acceptance_once_and_verifies_artifact_change(tmp_path: Path) -> None:
    target = tmp_path / "scene.txt"
    target.write_bytes(b"old fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="save", current_directory=str(tmp_path)))
    backend.save_bytes = b"new saved fixture"
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    checks: list[int] = []

    receipt = adapter.apply_result(
        context,
        target_path=str(target),
        item_kind="file",
        item_id=77,
        root_id="root-a",
        stable_file_id="stable-a",
        displayed_path=str(target),
        query_text="scene",
        activation_generation=9,
        action_generation=12,
        result_generation=9,
        revalidate_target=lambda: checks.append(1),
        accept_save=True,
    )

    assert checks == [1, 1, 1]
    assert backend.file_name_calls == [target.name]
    assert backend.save_accept_calls == 1
    assert target.read_bytes() == backend.save_bytes
    assert receipt.action == "save"
    assert receipt.accepted_by_dialog is True
    assert receipt.dispatch_happened is True
    assert receipt.saved_path_after == str(target)
    assert receipt.overwrite_confirmed is True
    assert receipt.artifact_changed is True
    assert receipt.saved_size_bytes == len(backend.save_bytes)
    assert receipt.saved_sha256 == hashlib.sha256(backend.save_bytes).hexdigest()
    assert receipt.saved_hash_verified is True
    assert receipt.acceptance_may_have_occurred is False


def test_save_file_receipt_hashes_destination_larger_than_snapshot_threshold(tmp_path: Path) -> None:
    target = tmp_path / "large-scene.bin"
    target.write_bytes(b"old fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="save", current_directory=str(tmp_path)))
    backend.save_bytes = b"S" * (8 * 1024 * 1024 + 1)
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    receipt = adapter.apply_result(
        context,
        target_path=str(target),
        item_kind="file",
        item_id=77,
        root_id="root-a",
        stable_file_id="stable-a",
        displayed_path=str(target),
        query_text="large-scene",
        activation_generation=9,
        accept_save=True,
    )

    assert receipt.saved_size_bytes == len(backend.save_bytes)
    assert receipt.saved_sha256 == hashlib.sha256(backend.save_bytes).hexdigest()
    assert receipt.saved_hash_verified is True
    assert receipt.saved_file_id


def test_save_file_refuses_verified_receipt_when_destination_changes_during_final_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "racing-save.txt"
    target.write_bytes(b"old fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="save", current_directory=str(tmp_path)))
    backend.save_bytes = b"new saved fixture"
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    real_open = open
    target_read_opens = 0

    class _MutatingStream:
        def __init__(self, path: str, mode: str) -> None:
            self.stream = real_open(path, mode)
            self.mutated = False

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self.stream.__exit__(exc_type, exc_value, traceback)

        def __getattr__(self, name: str):
            return getattr(self.stream, name)

        def read(self, size: int = -1) -> bytes:
            chunk = self.stream.read(size)
            if not self.mutated:
                self.mutated = True
                target.write_bytes(b"foreign concurrent bytes")
            return chunk

    def mutating_open(path: str, mode: str = "r", *args, **kwargs):
        nonlocal target_read_opens
        if os.path.normcase(os.path.abspath(os.fspath(path))) == os.path.normcase(str(target)) and mode == "rb":
            target_read_opens += 1
            if target_read_opens == 4:
                return _MutatingStream(path, mode)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(dialog_module, "open", mutating_open, raising=False)
    with pytest.raises(WindowsFileDialogError) as captured:
        adapter.apply_result(
            context,
            target_path=str(target),
            item_kind="file",
            item_id=77,
            root_id="root-a",
            stable_file_id="stable-a",
            displayed_path=str(target),
            query_text="racing-save",
            activation_generation=9,
            accept_save=True,
        )

    assert captured.value.code == "save_verification_unstable"
    assert captured.value.ambiguous is True
    assert captured.value.acceptance_may_have_occurred is True
    assert captured.value.receipt is not None
    assert captured.value.receipt.dispatch_happened is True
    assert backend.save_accept_calls == 1
    assert target_read_opens >= 4
    assert target.read_bytes() == b"foreign concurrent bytes"


def test_file_snapshot_rejects_mutation_during_hash_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "racing-file.txt"
    target.write_bytes(b"before")
    real_open = open

    class _MutatingStream:
        def __init__(self, path: str, mode: str) -> None:
            self.stream = real_open(path, mode)
            self.mutated = False

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self.stream.__exit__(exc_type, exc_value, traceback)

        def __getattr__(self, name: str):
            return getattr(self.stream, name)

        def read(self, size: int = -1) -> bytes:
            chunk = self.stream.read(size)
            if not self.mutated:
                self.mutated = True
                target.write_bytes(b"after with a different size")
            return chunk

    def mutating_open(path: str, mode: str = "r", *args, **kwargs):
        if os.path.normcase(os.path.abspath(os.fspath(path))) == os.path.normcase(str(target)) and mode == "rb":
            return _MutatingStream(path, mode)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(dialog_module, "open", mutating_open, raising=False)
    with pytest.raises(WindowsFileDialogError) as captured:
        dialog_module._snapshot_file(str(target), digest_limit_bytes=None)

    assert captured.value.code == "target_changed"
    assert target.read_bytes() == b"after with a different size"


def test_save_file_cancel_before_acceptance_never_invokes_save(tmp_path: Path) -> None:
    target = tmp_path / "scene.txt"
    target.write_bytes(b"old fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="save", current_directory=str(tmp_path)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    cancelled = threading.Event()
    checks = 0

    def revalidate() -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            cancelled.set()

    with pytest.raises(WindowsFileDialogError) as captured:
        adapter.apply_result(
            context,
            target_path=str(target),
            item_kind="file",
            item_id=77,
            root_id="root-a",
            stable_file_id="stable-a",
            displayed_path=str(target),
            query_text="scene",
            activation_generation=9,
            revalidate_target=revalidate,
            cancel_event=cancelled,
            accept_save=True,
        )

    assert captured.value.code == "cancelled"
    assert captured.value.ambiguous is False
    assert captured.value.acceptance_may_have_occurred is False
    assert captured.value.receipt is not None
    assert captured.value.receipt.dispatch_happened is False
    assert backend.save_accept_calls == 0
    assert target.read_bytes() == b"old fixture"


@pytest.mark.parametrize("error_code", ["save_delivery_timeout", "overwrite_requires_confirmation"])
def test_save_file_acceptance_ambiguity_is_never_retried(tmp_path: Path, error_code: str) -> None:
    target = tmp_path / "scene.txt"
    target.write_bytes(b"old fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="save", current_directory=str(tmp_path)))
    backend.save_accept_error = WindowsFileDialogError(
        error_code,
        "fixture unresolved confirmation after semantic Save invocation",
        ambiguous=True,
        acceptance_may_have_occurred=True,
    )
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    with pytest.raises(WindowsFileDialogError) as captured:
        adapter.apply_result(
            context,
            target_path=str(target),
            item_kind="file",
            item_id=77,
            root_id="root-a",
            stable_file_id="stable-a",
            displayed_path=str(target),
            query_text="scene",
            activation_generation=9,
            accept_save=True,
        )

    assert captured.value.code == error_code
    assert captured.value.ambiguous is True
    assert captured.value.acceptance_may_have_occurred is True
    assert captured.value.receipt is not None
    assert captured.value.receipt.action == "save"
    assert captured.value.receipt.dispatch_happened is True
    assert captured.value.receipt.acceptance_may_have_occurred is True
    assert backend.save_accept_calls == 1


def test_save_dialog_close_without_artifact_change_is_ambiguous_and_never_replayed(tmp_path: Path) -> None:
    target = tmp_path / "scene.txt"
    target.write_bytes(b"unchanged fixture")
    backend = _FakeBackend(_descriptor(dialog_kind="save", current_directory=str(tmp_path)))
    backend.save_write_artifact = False
    adapter = WindowsFileDialogAdapter(backend=backend, default_timeout_seconds=0.25)
    context = adapter.capture_foreground()
    assert context is not None

    with pytest.raises(WindowsFileDialogError) as captured:
        adapter.apply_result(
            context,
            target_path=str(target),
            item_kind="file",
            item_id=77,
            root_id="root-a",
            stable_file_id="stable-a",
            displayed_path=str(target),
            query_text="scene",
            activation_generation=9,
            accept_save=True,
        )

    assert captured.value.code == "save_delivery_unproven"
    assert captured.value.ambiguous is True
    assert captured.value.acceptance_may_have_occurred is True
    assert captured.value.receipt is not None
    assert captured.value.receipt.action == "save"
    assert captured.value.receipt.dispatch_happened is True
    assert backend.save_accept_calls == 1
    assert target.read_bytes() == b"unchanged fixture"


def test_save_folder_result_commits_exact_prepared_new_filename_without_overwrite(tmp_path: Path) -> None:
    start = tmp_path / "start"
    target_dir = tmp_path / "destination"
    start.mkdir()
    target_dir.mkdir()
    backend = _FakeBackend(_descriptor(dialog_kind="save", current_directory=str(start)))
    backend.initial_file_name = "new résumé Ω.txt"
    backend.save_bytes = b"new file contents"
    backend.save_overwrite_confirmed = False
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    checks: list[int] = []

    receipt = adapter.apply_result(
        context,
        target_path=str(target_dir),
        item_kind="folder",
        item_id=88,
        root_id="root-a",
        stable_file_id="stable-folder",
        displayed_path=str(target_dir),
        query_text="destination",
        activation_generation=10,
        revalidate_target=lambda: checks.append(1),
        accept_save=True,
    )

    expected = target_dir / backend.initial_file_name
    assert checks == [1, 1]
    assert backend.navigation_calls == [str(target_dir)]
    assert backend.file_name_calls == [backend.initial_file_name]
    assert backend.save_accept_calls == 1
    assert expected.read_bytes() == backend.save_bytes
    assert receipt.action == "save"
    assert receipt.target_path == str(expected)
    assert receipt.saved_path_after == str(expected)
    assert receipt.prepared_filename_after == backend.initial_file_name
    assert receipt.accepted_by_dialog is True
    assert receipt.overwrite_confirmed is False
    assert receipt.artifact_changed is True


def test_native_open_acceptance_tolerates_bounded_uia_teardown_before_hwnd_destroy() -> None:
    state = {"invoked": False, "dialog_checks": 0}
    context = dialog_module.WindowsFileDialogContext(
        schema="listary_windows_file_context_v1",
        context_token="windows-dialog:fixture",
        generation=1,
        hwnd=101,
        process_id=202,
        thread_id=303,
        owner_hwnd=404,
        runtime_id=(42, 101),
        window_class="#32770",
        title="Open",
        dialog_kind="open",
        current_directory="C:/fixture",
        process_start_time_ns=123456789,
    )
    descriptor = _descriptor(current_directory="C:/fixture")

    class _Invoke:
        def Invoke(self) -> None:
            state["invoked"] = True

    class _Button:
        NativeWindowHandle = 501
        ControlTypeName = "ButtonControl"
        IsEnabled = True

        @staticmethod
        def GetRuntimeId():
            return (42, 501)

        @staticmethod
        def GetInvokePattern():
            return _Invoke()

    class _Automation:
        @staticmethod
        def ControlFromHandle(hwnd: int):
            assert hwnd == 501
            return _Button()

    class _Win32Gui:
        @staticmethod
        def GetDlgItem(hwnd: int, control_id: int) -> int:
            assert hwnd == 101 and control_id == 1
            return 501

        @staticmethod
        def GetClassName(hwnd: int) -> str:
            assert hwnd == 501
            return "Button"

        @staticmethod
        def GetWindowText(hwnd: int) -> str:
            assert hwnd == 501
            return "&Open"

        @staticmethod
        def IsWindow(hwnd: int) -> bool:
            if hwnd == 404:
                return True
            assert hwnd == 101
            if not state["invoked"]:
                return True
            state["dialog_checks"] += 1
            return state["dialog_checks"] < 3

        @staticmethod
        def IsWindowEnabled(hwnd: int) -> bool:
            assert hwnd == 404
            return True

    backend = object.__new__(dialog_module._WindowsUiAutomationBackend)
    backend.automation = _Automation()
    backend.win32gui = _Win32Gui()
    backend.make_foreground = lambda hwnd, *, timeout_seconds: hwnd == 101 and timeout_seconds > 0
    backend.foreground_window = lambda: 101
    backend._require_context_current = lambda _context: descriptor
    backend._require_control_in_context = (
        lambda _context, control, **_kwargs: tuple(control.GetRuntimeId())
    )
    backend.describe = lambda _hwnd: None if state["invoked"] else descriptor
    backend.context_is_current = lambda _context: not state["invoked"]

    backend.accept_open_result(
        context,
        deadline=time.monotonic() + 1.0,
        cancelled=None,
    )

    assert state["invoked"] is True
    assert state["dialog_checks"] >= 3


def test_native_save_overwrite_waits_for_visible_popup_but_requires_human_confirmation() -> None:
    state = {
        "save_invoked": False,
        "visibility_checks": 0,
        "yes_invoked": False,
        "dialog_closed": False,
    }
    context = dialog_module.WindowsFileDialogContext(
        schema="listary_windows_file_context_v1",
        context_token="windows-dialog:fixture-save",
        generation=1,
        hwnd=101,
        process_id=202,
        thread_id=303,
        owner_hwnd=404,
        runtime_id=(42, 101),
        window_class="#32770",
        title="Save As",
        dialog_kind="save",
        current_directory="C:/fixture",
        process_start_time_ns=123456789,
    )
    descriptor = _descriptor(dialog_kind="save", current_directory="C:/fixture")

    class _Invoke:
        def __init__(self, kind: str) -> None:
            self.kind = kind

        def Invoke(self) -> None:
            if self.kind == "save":
                state["save_invoked"] = True
            else:
                state["yes_invoked"] = True
                state["dialog_closed"] = True

    class _Control:
        def __init__(
            self,
            *,
            control_type: str,
            name: str = "",
            automation_id: str = "",
            hwnd: int = 0,
            process_id: int = 202,
            children: tuple[object, ...] = (),
        ) -> None:
            self.ControlTypeName = control_type
            self.Name = name
            self.AutomationId = automation_id
            self.NativeWindowHandle = hwnd
            self.ProcessId = process_id
            self.IsEnabled = True
            self._children = children

        def GetRuntimeId(self):
            return (42, int(self.NativeWindowHandle or 1))

        def GetInvokePattern(self):
            return _Invoke("save" if self.NativeWindowHandle == 501 else "yes")

        def GetChildren(self):
            return list(self._children)

    overwrite_text = _Control(
        control_type="TextControl",
        name="scene.txt already exists.\r\nDo you want to replace it?",
    )
    yes_button = _Control(
        control_type="ButtonControl",
        name="Yes",
        automation_id="CommandButton_6",
        hwnd=601,
    )
    popup_root = _Control(
        control_type="WindowControl",
        hwnd=600,
        children=(overwrite_text, yes_button),
    )
    save_button = _Control(
        control_type="ButtonControl",
        name="Save",
        hwnd=501,
    )

    class _Automation:
        @staticmethod
        def ControlFromHandle(hwnd: int):
            if hwnd == 501:
                return save_button
            if hwnd == 600:
                return popup_root
            raise AssertionError(hwnd)

    class _Win32Gui:
        @staticmethod
        def GetDlgItem(hwnd: int, control_id: int) -> int:
            assert hwnd == 101 and control_id == 1
            return 501

        @staticmethod
        def GetClassName(hwnd: int) -> str:
            return {501: "Button", 600: "#32770", 601: "Button"}[hwnd]

        @staticmethod
        def GetWindowText(hwnd: int) -> str:
            assert hwnd == 501
            return "&Save"

        @staticmethod
        def IsWindow(hwnd: int) -> bool:
            if hwnd == 101:
                return not state["dialog_closed"]
            if hwnd == 404:
                return True
            return True

        @staticmethod
        def IsWindowEnabled(hwnd: int) -> bool:
            assert hwnd == 404
            return state["dialog_closed"]

        @staticmethod
        def GetWindow(hwnd: int, relation: int) -> int:
            if hwnd == 101 and relation == 6:
                return 600 if state["save_invoked"] else 101
            if hwnd == 600 and relation == 4:
                return 101
            return 0

        @staticmethod
        def IsWindowVisible(hwnd: int) -> bool:
            assert hwnd == 600
            state["visibility_checks"] += 1
            return state["visibility_checks"] >= 2

        @staticmethod
        def GetAncestor(hwnd: int, relation: int) -> int:
            assert hwnd == 601 and relation == 2
            return 600

    class _Win32Process:
        @staticmethod
        def GetWindowThreadProcessId(hwnd: int):
            assert hwnd == 600
            return (303, 202)

    backend = object.__new__(dialog_module._WindowsUiAutomationBackend)
    backend.automation = _Automation()
    backend.win32gui = _Win32Gui()
    backend.win32process = _Win32Process()
    backend.win32con = type(
        "_Win32Con",
        (),
        {"GW_ENABLEDPOPUP": 6, "GW_OWNER": 4, "GA_ROOT": 2},
    )()
    backend.make_foreground = lambda hwnd, *, timeout_seconds: hwnd == 101 and timeout_seconds > 0
    backend.foreground_window = lambda: 101
    backend._require_context_current = lambda _context: descriptor
    backend._require_control_in_context = (
        lambda _context, control, **_kwargs: tuple(control.GetRuntimeId())
    )
    backend.read_file_name = lambda _context: "scene.txt"

    with pytest.raises(WindowsFileDialogError) as captured:
        backend.accept_save_result(
            context,
            target_path="C:/fixture/scene.txt",
            filename="scene.txt",
            deadline=time.monotonic() + 1.0,
            cancelled=None,
        )

    assert captured.value.code == "overwrite_requires_confirmation"
    assert captured.value.ambiguous is True
    assert captured.value.acceptance_may_have_occurred is True
    assert state["save_invoked"] is True
    assert state["visibility_checks"] >= 2
    assert state["yes_invoked"] is False
    assert state["dialog_closed"] is False


def test_unavailable_target_fails_before_native_change(tmp_path: Path) -> None:
    missing = tmp_path / "missing.mov"
    backend = _FakeBackend(_descriptor(current_directory=str(tmp_path)))
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, context, missing, kind="file")

    assert captured.value.code == "target_unavailable"
    assert backend.navigation_calls == []
    assert backend.file_name_calls == []


def test_permission_denial_is_reported_before_dialog_action(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "denied"
    target.mkdir()
    original_scandir = dialog_module.os.scandir

    def denied_scandir(path):
        if dialog_module._paths_equal(path, target):
            raise PermissionError("fixture denied")
        return original_scandir(path)

    monkeypatch.setattr(dialog_module.os, "scandir", denied_scandir)

    with pytest.raises(WindowsFileDialogError) as captured:
        dialog_module._probe_target(str(target), expect_directory=True)

    assert captured.value.code == "permission_denied"
    assert captured.value.ambiguous is False


def test_ambiguous_post_dispatch_failure_is_propagated_without_retry(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    backend = _FakeBackend(_descriptor(current_directory=str(tmp_path)))
    backend.navigation_error = WindowsFileDialogError(
        "navigation_timeout",
        "fixture timeout after dispatch",
        ambiguous=True,
    )
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None

    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, context, target, kind="folder")

    assert captured.value.code == "navigation_timeout"
    assert captured.value.ambiguous is True
    assert captured.value.receipt is not None
    assert captured.value.receipt.ambiguous is True
    assert captured.value.receipt.code == "navigation_timeout"
    assert backend.navigation_calls == [str(target)]


def test_concurrent_native_action_is_rejected_without_entering_backend(tmp_path: Path) -> None:
    start = tmp_path / "start"
    first_target = tmp_path / "first"
    second_target = tmp_path / "second"
    start.mkdir()
    first_target.mkdir()
    second_target.mkdir()
    backend = _FakeBackend(_descriptor(current_directory=str(start)))
    backend.navigation_release = threading.Event()
    adapter = WindowsFileDialogAdapter(backend=backend)
    context = adapter.capture_foreground()
    assert context is not None
    first_errors: list[BaseException] = []

    def run_first() -> None:
        try:
            _apply(adapter, context, first_target, kind="folder")
        except BaseException as exc:
            first_errors.append(exc)

    first_thread = threading.Thread(target=run_first, daemon=True)
    first_thread.start()
    assert backend.navigation_entered.wait(1.0)

    with pytest.raises(WindowsFileDialogError) as captured:
        _apply(adapter, context, second_target, kind="folder")

    assert captured.value.code == "action_in_progress"
    assert backend.navigation_calls == [str(first_target)]
    backend.navigation_release.set()
    first_thread.join(timeout=2.0)
    assert first_thread.is_alive() is False
    assert first_errors == []

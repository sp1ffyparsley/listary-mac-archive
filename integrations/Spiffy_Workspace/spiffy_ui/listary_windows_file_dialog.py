"""Semantic Windows Open/Save dialog adapter for the Listary launcher.

The shared Listary agent/Core owns result identity.  This module owns only the
last native boundary: detecting one foreground Windows Common Item Dialog,
navigating that exact dialog, proving the resulting folder/filename state, and
semantically accepting an exact Open/Save action when explicitly requested.
It never opens Listary state or indexes files.
"""

from __future__ import annotations

from contextlib import nullcontext
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import hashlib
import os
import stat
import sys
import threading
import time
from typing import Any, Callable


_WINDOWS_DIALOG_CLASS = "#32770"
_ADDRESS_AUTOMATION_ID = "1001"
_ADDRESS_EDIT_AUTOMATION_ID = "41477"
_FILE_NAME_LABEL = "File name:"
_FILE_NAME_CONTROL_ID = 1148
_SAVE_FILE_NAME_CONTROL_ID = 1001
_SHELL_VIEW_CONTROL_ID = 1121
_SHGFI_DISPLAYNAME = 0x00000200


class WindowsFileDialogError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        ambiguous: bool = False,
        acceptance_may_have_occurred: bool = False,
        receipt: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "file_dialog_error")
        self.ambiguous = bool(ambiguous)
        self.acceptance_may_have_occurred = bool(acceptance_may_have_occurred)
        self.receipt = receipt


@dataclass(frozen=True)
class WindowsFileDialogContext:
    schema: str
    context_token: str
    generation: int
    hwnd: int
    process_id: int
    thread_id: int
    owner_hwnd: int
    runtime_id: tuple[int, ...]
    window_class: str
    title: str
    dialog_kind: str
    current_directory: str | None
    process_start_time_ns: int = 0


@dataclass(frozen=True)
class WindowsFileDialogReceipt:
    schema: str
    outcome: str
    action: str
    dialog_kind: str
    context_token: str
    context_generation: int
    activation_generation: int
    query_text: str
    item_id: int
    root_id: str
    stable_file_id: str
    displayed_path: str
    target_path: str
    directory_before: str | None
    directory_after: str | None
    selected_path_after: str | None
    action_latency_ms: float
    accepted_by_dialog: bool
    ambiguous: bool
    detail: str
    action_generation: int = 0
    result_generation: int = 0
    item_kind: str = ""
    prepared_filename_after: str | None = None
    acceptance_may_have_occurred: bool = False
    dispatch_happened: bool = False
    saved_path_after: str | None = None
    overwrite_confirmed: bool = False
    artifact_changed: bool = False
    saved_size_bytes: int | None = None
    saved_modified_ns: int | None = None
    saved_file_id: str = ""
    saved_sha256: str = ""
    saved_hash_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WindowsFileDialogFailureReceipt:
    schema: str
    outcome: str
    code: str
    action: str
    dialog_kind: str
    context_token: str
    context_generation: int
    activation_generation: int
    query_text: str
    item_id: int
    root_id: str
    stable_file_id: str
    displayed_path: str
    target_path: str
    action_latency_ms: float
    accepted_by_dialog: bool
    ambiguous: bool
    detail: str
    action_generation: int = 0
    result_generation: int = 0
    item_kind: str = ""
    acceptance_may_have_occurred: bool = False
    dispatch_happened: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _DialogDescriptor:
    hwnd: int
    process_id: int
    thread_id: int
    owner_hwnd: int
    runtime_id: tuple[int, ...]
    window_class: str
    title: str
    dialog_kind: str
    current_directory: str | None
    process_start_time_ns: int = 0


@dataclass(frozen=True)
class _FileSnapshot:
    file_id: str
    size_bytes: int
    modified_ns: int
    sha256: str


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HICON),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


def _descriptor_matches_context(
    descriptor: _DialogDescriptor,
    context: WindowsFileDialogContext,
) -> bool:
    return (
        descriptor.hwnd == context.hwnd
        and descriptor.process_id == context.process_id
        and descriptor.thread_id == context.thread_id
        and descriptor.owner_hwnd == context.owner_hwnd
        and descriptor.runtime_id == context.runtime_id
        and descriptor.window_class == context.window_class
        and descriptor.dialog_kind == context.dialog_kind
        and descriptor.process_start_time_ns == context.process_start_time_ns
    )


def _path_key(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(value))))


def _send_keys_literal(value: str) -> str:
    """Escape literal braces for uiautomation's SendKeys grammar."""

    return "".join("{{}" if char == "{" else "{}}" if char == "}" else char for char in str(value))


def _paths_equal(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    try:
        return _path_key(left) == _path_key(right)
    except (OSError, TypeError, ValueError):
        return False


def _address_name_path(name: str) -> str | None:
    text = str(name or "").strip()
    if not text:
        return None
    _label, separator, value = text.partition(":")
    if not separator:
        return None
    value = value.strip()
    if not value or not os.path.isabs(value):
        return None
    return value


def _probe_target(path: str, *, expect_directory: bool) -> None:
    target = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isabs(target):
        raise WindowsFileDialogError("target_invalid", "The dialog target must be an absolute path.")
    try:
        info = os.stat(target)
    except PermissionError as exc:
        raise WindowsFileDialogError("permission_denied", f"Access is denied: {target}") from exc
    except FileNotFoundError as exc:
        raise WindowsFileDialogError("target_unavailable", f"The target no longer exists: {target}") from exc
    except OSError as exc:
        raise WindowsFileDialogError("target_unavailable", f"The target is unavailable: {target}: {exc}") from exc
    if expect_directory and not stat.S_ISDIR(info.st_mode):
        raise WindowsFileDialogError("target_invalid", f"Expected a folder target: {target}")
    if not expect_directory and not stat.S_ISREG(info.st_mode):
        raise WindowsFileDialogError("target_invalid", f"Expected a file target: {target}")
    probe_directory = target if expect_directory else os.path.dirname(target)
    try:
        with os.scandir(probe_directory):
            pass
    except PermissionError as exc:
        raise WindowsFileDialogError("permission_denied", f"Access is denied: {probe_directory}") from exc
    except OSError as exc:
        raise WindowsFileDialogError("target_unavailable", f"The target folder is unavailable: {probe_directory}: {exc}") from exc


def _snapshot_stat_signature(info: os.stat_result) -> tuple[str, int, int]:
    return (
        f"{int(info.st_dev):x}:{int(info.st_ino):x}",
        int(info.st_size),
        int(info.st_mtime_ns),
    )


def _snapshot_file(path: str, *, digest_limit_bytes: int | None = 8 * 1024 * 1024) -> _FileSnapshot:
    target = os.path.abspath(os.path.expanduser(str(path)))
    try:
        info = os.stat(target)
    except PermissionError as exc:
        raise WindowsFileDialogError("permission_denied", f"Access is denied: {target}") from exc
    except FileNotFoundError as exc:
        raise WindowsFileDialogError("target_unavailable", f"The target no longer exists: {target}") from exc
    except OSError as exc:
        raise WindowsFileDialogError("target_unavailable", f"The target is unavailable: {target}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise WindowsFileDialogError("target_invalid", f"Expected a file target: {target}")
    digest = ""
    should_digest = digest_limit_bytes is None or int(info.st_size) <= max(0, int(digest_limit_bytes))
    if should_digest:
        try:
            hasher = hashlib.sha256()
            with open(target, "rb") as stream:
                opened_info = os.fstat(stream.fileno())
                if _snapshot_stat_signature(opened_info) != _snapshot_stat_signature(info):
                    raise WindowsFileDialogError(
                        "target_changed",
                        "The target was replaced before content verification began.",
                    )
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
                finished_info = os.fstat(stream.fileno())
            current_info = os.stat(target)
            if not (
                _snapshot_stat_signature(info)
                == _snapshot_stat_signature(opened_info)
                == _snapshot_stat_signature(finished_info)
                == _snapshot_stat_signature(current_info)
            ):
                raise WindowsFileDialogError(
                    "target_changed",
                    "The target changed while its SHA-256 was being computed.",
                )
            info = current_info
            digest = hasher.hexdigest()
        except WindowsFileDialogError:
            raise
        except PermissionError as exc:
            raise WindowsFileDialogError("permission_denied", f"Access is denied: {target}") from exc
        except OSError as exc:
            raise WindowsFileDialogError("target_unavailable", f"The target cannot be read: {target}: {exc}") from exc
    else:
        try:
            current_info = os.stat(target)
        except PermissionError as exc:
            raise WindowsFileDialogError("permission_denied", f"Access is denied: {target}") from exc
        except FileNotFoundError as exc:
            raise WindowsFileDialogError("target_unavailable", f"The target no longer exists: {target}") from exc
        except OSError as exc:
            raise WindowsFileDialogError("target_unavailable", f"The target is unavailable: {target}: {exc}") from exc
        if _snapshot_stat_signature(info) != _snapshot_stat_signature(current_info):
            raise WindowsFileDialogError("target_changed", "The target changed while its metadata snapshot was being read.")
        info = current_info
    return _FileSnapshot(
        file_id=_snapshot_stat_signature(info)[0],
        size_bytes=int(info.st_size),
        modified_ns=int(info.st_mtime_ns),
        sha256=digest,
    )


def _snapshot_changed(before: _FileSnapshot, after: _FileSnapshot) -> bool:
    if before.file_id != after.file_id:
        return True
    if before.size_bytes != after.size_bytes or before.modified_ns != after.modified_ns:
        return True
    if before.sha256 and after.sha256 and before.sha256 != after.sha256:
        return True
    return False


def _verified_saved_snapshot(path: str, observed: _FileSnapshot) -> _FileSnapshot:
    """Return a full stable destination hash after Save was dispatched exactly once."""

    try:
        verified = _snapshot_file(path, digest_limit_bytes=None)
    except WindowsFileDialogError as exc:
        raise WindowsFileDialogError(
            "save_verification_unstable",
            "The Save destination could not be hashed from a stable file identity; Save was not replayed.",
            ambiguous=True,
            acceptance_may_have_occurred=True,
        ) from exc
    if _snapshot_changed(observed, verified) or len(verified.sha256) != 64:
        raise WindowsFileDialogError(
            "save_verification_unstable",
            "The Save destination changed during content verification; Save was not replayed.",
            ambiguous=True,
            acceptance_may_have_occurred=True,
        )
    return verified


class _WindowsUiAutomationBackend:
    """Small wrapper so policy/currentness logic remains unit-testable."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WindowsFileDialogError("unsupported_platform", "Windows file-dialog integration is available only on Windows.")
        import uiautomation as automation
        import win32api
        import win32con
        import win32gui
        import win32process

        self.automation = automation
        self.win32api = win32api
        self.win32con = win32con
        self.win32gui = win32gui
        self.win32process = win32process

    def automation_context(self):
        """Initialize COM/UIA for whichever thread performs this operation."""

        return self.automation.UIAutomationInitializerInThread()

    def foreground_window(self) -> int:
        return int(self.win32gui.GetForegroundWindow() or 0)

    def _dialog_root(self, hwnd: int):
        return self.automation.ControlFromHandle(int(hwnd))

    def _find_descendant(self, hwnd: int, *, control_id: int, class_name: str) -> int:
        matches: list[int] = []

        def collect(child: int, _extra: Any) -> bool:
            try:
                if (
                    int(self.win32gui.GetDlgCtrlID(child)) == int(control_id)
                    and str(self.win32gui.GetClassName(child) or "") == class_name
                ):
                    matches.append(int(child))
            except Exception:
                pass
            return True

        try:
            self.win32gui.EnumChildWindows(int(hwnd), collect, None)
        except Exception:
            return 0
        return matches[0] if matches else 0

    def _primary_button_name(self, hwnd: int) -> str:
        try:
            button = int(self.win32gui.GetDlgItem(int(hwnd), 1) or 0)
            if not button or str(self.win32gui.GetClassName(button) or "") != "Button":
                return ""
            return str(self.win32gui.GetWindowText(button) or "").replace("&", "").strip()
        except Exception:
            return ""

    def current_directory(self, hwnd: int) -> str | None:
        toolbar = self._find_descendant(
            hwnd,
            control_id=int(_ADDRESS_AUTOMATION_ID),
            class_name="ToolbarWindow32",
        )
        if not toolbar:
            return None
        return _address_name_path(str(self.win32gui.GetWindowText(toolbar) or ""))

    def _process_start_time_ns(self, process_id: int) -> int:
        handle = None
        try:
            handle = self.win32api.OpenProcess(
                self.win32con.PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                int(process_id),
            )
            creation = self.win32process.GetProcessTimes(handle)["CreationTime"]
            return int(round(float(creation.timestamp()) * 1_000_000_000))
        except Exception:
            return 0
        finally:
            if handle is not None:
                try:
                    handle.Close()
                except Exception:
                    pass

    def describe(self, hwnd: int) -> _DialogDescriptor | None:
        hwnd = int(hwnd or 0)
        if not hwnd or not self.win32gui.IsWindow(hwnd) or not self.win32gui.IsWindowVisible(hwnd):
            return None
        window_class = str(self.win32gui.GetClassName(hwnd) or "")
        if window_class != _WINDOWS_DIALOG_CLASS:
            return None
        thread_id, process_id = self.win32process.GetWindowThreadProcessId(hwnd)
        process_start_time_ns = self._process_start_time_ns(process_id)
        if process_start_time_ns <= 0:
            return None
        root = self._dialog_root(hwnd)
        try:
            runtime_id = tuple(int(value) for value in root.GetRuntimeId())
        except Exception:
            return None
        primary = self._primary_button_name(hwnd).casefold()
        if primary == "open":
            dialog_kind = "open"
        elif primary == "save":
            dialog_kind = "save"
        else:
            return None
        return _DialogDescriptor(
            hwnd=hwnd,
            process_id=int(process_id),
            thread_id=int(thread_id),
            owner_hwnd=int(self.win32gui.GetWindow(hwnd, self.win32con.GW_OWNER) or 0),
            runtime_id=runtime_id,
            window_class=window_class,
            title=str(self.win32gui.GetWindowText(hwnd) or ""),
            dialog_kind=dialog_kind,
            current_directory=self.current_directory(hwnd),
            process_start_time_ns=process_start_time_ns,
        )

    def context_is_current(self, context: WindowsFileDialogContext) -> bool:
        if (
            not context.owner_hwnd
            or not self.win32gui.IsWindow(context.owner_hwnd)
            or not self.win32gui.IsWindow(context.hwnd)
            or not self.win32gui.IsWindowVisible(context.hwnd)
            or not self.win32gui.IsWindowEnabled(context.hwnd)
        ):
            return False
        try:
            if int(self.win32gui.GetWindow(context.hwnd, self.win32con.GW_OWNER) or 0) != context.owner_hwnd:
                return False
            # A native Common Item Dialog is modal to its owner: the owner is
            # disabled while the dialog remains enabled.  On current Windows 10
            # Notepad, GW_ENABLEDPOPUP returns 0 for this real shell dialog, while
            # GetLastActivePopup returns the exact dialog HWND.  Require the full
            # modal/owner relationship and accept either Win32 popup proof; this
            # survives Listary taking foreground focus without treating an old
            # still-visible #32770 window as current.
            if self.win32gui.IsWindowEnabled(context.owner_hwnd):
                return False
            enabled_popup = int(
                self.win32gui.GetWindow(context.owner_hwnd, self.win32con.GW_ENABLEDPOPUP) or 0
            )
            last_active_popup = int(ctypes.windll.user32.GetLastActivePopup(int(context.owner_hwnd)) or 0)
            return context.hwnd in {enabled_popup, last_active_popup}
        except Exception:
            return False

    def make_foreground(self, hwnd: int, *, timeout_seconds: float) -> bool:
        if self.foreground_window() == int(hwnd):
            return True
        # The Qt launcher legitimately holds foreground while handing an exact
        # captured dialog back to its native owner. A different foreground
        # process means the human switched applications. Do not attach that
        # process's input thread or bring a stale modal over their current work.
        current_foreground = self.foreground_window()
        if not current_foreground:
            return False
        try:
            _thread, foreground_process = self.win32process.GetWindowThreadProcessId(current_foreground)
            if int(foreground_process) != os.getpid():
                return False
            if self.foreground_window() != current_foreground:
                return False
        except Exception:
            return False
        deadline = time.monotonic() + max(0.05, float(timeout_seconds))
        attached_threads: list[int] = []
        try:
            current_thread = int(ctypes.windll.kernel32.GetCurrentThreadId())
            target_thread, _target_process = self.win32process.GetWindowThreadProcessId(int(hwnd))
            foreground = self.foreground_window()
            foreground_thread = (
                int(self.win32process.GetWindowThreadProcessId(foreground)[0])
                if foreground
                else 0
            )
            for thread_id in (int(target_thread), foreground_thread):
                if not thread_id or thread_id == current_thread or thread_id in attached_threads:
                    continue
                if ctypes.windll.user32.AttachThreadInput(current_thread, thread_id, True):
                    attached_threads.append(thread_id)
            if self.foreground_window() != current_foreground:
                return False
            self.win32gui.ShowWindow(int(hwnd), self.win32con.SW_RESTORE)
            self.win32gui.BringWindowToTop(int(hwnd))
            self.win32gui.SetForegroundWindow(int(hwnd))
        except Exception:
            return False
        finally:
            if "current_thread" in locals():
                for thread_id in reversed(attached_threads):
                    try:
                        ctypes.windll.user32.AttachThreadInput(current_thread, thread_id, False)
                    except Exception:
                        pass
        while time.monotonic() < deadline:
            if self.foreground_window() == int(hwnd):
                return True
            time.sleep(0.01)
        return self.foreground_window() == int(hwnd)

    def _require_context_identity(self, context: WindowsFileDialogContext) -> _DialogDescriptor:
        descriptor = self.describe(context.hwnd)
        if descriptor is None or not _descriptor_matches_context(descriptor, context):
            raise WindowsFileDialogError(
                "stale_context",
                "The captured Open/Save dialog was replaced or changed identity.",
            )
        return descriptor

    def _require_context_current(self, context: WindowsFileDialogContext) -> _DialogDescriptor:
        descriptor = self._require_context_identity(context)
        if not self.context_is_current(context):
            raise WindowsFileDialogError(
                "stale_context",
                "The captured Open/Save dialog is no longer the owner's active modal.",
            )
        return descriptor

    @staticmethod
    def _runtime_id(control: Any) -> tuple[int, ...]:
        try:
            return tuple(int(value) for value in control.GetRuntimeId())
        except Exception:
            return ()

    def _require_control_in_context(
        self,
        context: WindowsFileDialogContext,
        control: Any,
        *,
        expected_runtime_id: tuple[int, ...] | None = None,
    ) -> tuple[int, ...]:
        if int(getattr(control, "ProcessId", 0) or 0) != context.process_id:
            raise WindowsFileDialogError("focus_changed", "The focused control belongs to a different process.")
        runtime_id = self._runtime_id(control)
        if not runtime_id or (expected_runtime_id is not None and runtime_id != expected_runtime_id):
            raise WindowsFileDialogError("focus_changed", "The focused dialog control changed identity.")
        try:
            top = control.GetTopLevelControl()
            top_runtime_id = self._runtime_id(top)
        except Exception as exc:
            raise WindowsFileDialogError("focus_changed", "The focused control has no verifiable dialog owner.") from exc
        if top_runtime_id != context.runtime_id:
            raise WindowsFileDialogError("focus_changed", "The focused control belongs to a different dialog.")
        native = int(getattr(control, "NativeWindowHandle", 0) or 0)
        if native and int(self.win32gui.GetAncestor(native, self.win32con.GA_ROOT) or 0) != context.hwnd:
            raise WindowsFileDialogError("focus_changed", "The focused control belongs to a different window.")
        return runtime_id

    def commit_directory_navigation(
        self,
        context: WindowsFileDialogContext,
        target: str,
        *,
        deadline: float,
        cancelled: threading.Event | None,
    ) -> tuple[str | None, bool]:
        """Return post-navigation directory and whether the one-shot commit ran."""

        if cancelled is not None and cancelled.is_set():
            raise WindowsFileDialogError("cancelled", "Dialog action was cancelled before navigation.")
        self._require_context_current(context)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self.make_foreground(context.hwnd, timeout_seconds=min(0.25, remaining)):
            raise WindowsFileDialogError("focus_changed", "The captured Open/Save dialog could not be foregrounded safely.")
        self._require_context_current(context)

        # The Windows Common Item Dialog exposes its editable shell address only
        # while the standard address command is active.  First focus its verified
        # Items View so the accelerator is scoped to this exact dialog rather than
        # whichever control another process last owned.  The accelerator is sent
        # only after exact HWND/currentness/control proof; all actual text is then
        # written and read back through the exact native address edit.
        items_before = self._items_view(context)
        items_before.SetFocus()
        focused_before = self.automation.GetFocusedControl()
        self._require_control_in_context(context, focused_before)
        self._require_context_current(context)
        self.automation.SendKeys("{Alt}d", waitTime=0.02)
        focused = None
        focus_deadline = min(deadline, time.monotonic() + 0.30)
        while time.monotonic() < focus_deadline:
            if cancelled is not None and cancelled.is_set():
                raise WindowsFileDialogError("cancelled", "Dialog action was cancelled while activating the address edit.")
            if self.foreground_window() != context.hwnd:
                raise WindowsFileDialogError("focus_changed", "The captured dialog lost focus while activating its address edit.")
            candidate = self.automation.GetFocusedControl()
            if (
                str(candidate.ControlTypeName or "") == "EditControl"
                and str(candidate.AutomationId or "") == _ADDRESS_EDIT_AUTOMATION_ID
            ):
                focused = candidate
                break
            time.sleep(0.01)
        if focused is None:
            raise WindowsFileDialogError("address_control_missing", "The captured dialog did not expose its semantic address edit.")
        focused_runtime_id = self._require_control_in_context(context, focused)
        self._require_context_current(context)
        address_hwnd = int(getattr(focused, "NativeWindowHandle", 0) or 0)
        if (
            not address_hwnd
            or str(self.win32gui.GetClassName(address_hwnd) or "") != "Edit"
            or int(self.win32gui.GetDlgCtrlID(address_hwnd)) != int(_ADDRESS_EDIT_AUTOMATION_ID)
        ):
            raise WindowsFileDialogError(
                "address_control_missing",
                "The captured dialog address edit has no verified native control handle.",
            )
        pattern = focused.GetValuePattern()
        pattern.SetValue(str(target))
        if not _paths_equal(str(pattern.Value or ""), target):
            raise WindowsFileDialogError("address_write_mismatch", "The dialog address edit did not retain the requested folder.")
        if cancelled is not None and cancelled.is_set():
            raise WindowsFileDialogError("cancelled", "Dialog action was cancelled before navigation commit.")
        self._require_context_current(context)
        if self.foreground_window() != context.hwnd:
            raise WindowsFileDialogError("focus_changed", "The captured dialog lost focus before navigation commit.")
        focused_after = self.automation.GetFocusedControl()
        self._require_control_in_context(
            context,
            focused_after,
            expected_runtime_id=focused_runtime_id,
        )
        focused_after_hwnd = int(getattr(focused_after, "NativeWindowHandle", 0) or 0)
        if focused_after_hwnd != address_hwnd:
            raise WindowsFileDialogError(
                "focus_changed",
                "The focused address edit changed native control identity before navigation commit.",
            )
        # The Common Item Dialog only commits address-bar navigation through its
        # normal keyboard command path; direct WM_KEYDOWN/WM_CHAR delivery to
        # the Edit does not navigate in the real Notepad-owned dialog.  Keep the
        # final UI-automation boundary narrow: bind the exact Address edit by
        # top-level RuntimeId + PID + native HWND, explicitly focus it, re-prove
        # that exact focus/current modal immediately before sending Enter, then
        # require semantic folder readback.  We never target the dialog/default
        # Open/Save button.
        focused.SetFocus()
        focused_ready = self.automation.GetFocusedControl()
        self._require_control_in_context(
            context,
            focused_ready,
            expected_runtime_id=focused_runtime_id,
        )
        if int(getattr(focused_ready, "NativeWindowHandle", 0) or 0) != address_hwnd:
            raise WindowsFileDialogError(
                "focus_changed",
                "The address edit lost exact native focus before navigation commit.",
            )
        self._require_context_current(context)
        # Bind the final navigation keystroke to the exact re-proven Address
        # edit rather than using the module-global key sender.  Control.SendKeys
        # focuses this exact UIA element immediately before delivery, narrowing
        # the only unavoidable keyboard boundary so Enter cannot intentionally
        # target the dialog's default Open/Save button.
        focused_ready.SendKeys("{Enter}", waitTime=0.02)
        dispatched = True
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled.is_set():
                raise WindowsFileDialogError(
                    "cancelled_after_dispatch",
                    "Dialog navigation was cancelled after the one-shot address commit; it was not replayed.",
                    ambiguous=True,
                )
            if not self.win32gui.IsWindow(context.hwnd):
                raise WindowsFileDialogError(
                    "dialog_closed_after_dispatch",
                    "The dialog closed during navigation; the action was not replayed.",
                    ambiguous=True,
                    acceptance_may_have_occurred=True,
                )
            try:
                descriptor = self._require_context_current(context)
            except WindowsFileDialogError as exc:
                raise WindowsFileDialogError(
                    "context_changed_after_dispatch",
                    "The dialog/owner context changed after navigation was dispatched; the action was not replayed.",
                    ambiguous=True,
                ) from exc
            current = descriptor.current_directory
            if current is not None and _paths_equal(current, target):
                return current, dispatched
            time.sleep(0.025)
        raise WindowsFileDialogError(
            "navigation_timeout",
            "The dialog did not confirm the requested folder before the timeout; navigation was not replayed.",
            ambiguous=True,
        )

    def _items_view(self, context: WindowsFileDialogContext) -> Any:
        self._require_context_current(context)
        shell_view = self._find_descendant(
            context.hwnd,
            control_id=_SHELL_VIEW_CONTROL_ID,
            class_name="SHELLDLL_DefView",
        )
        if not shell_view:
            raise WindowsFileDialogError(
                "items_view_missing",
                "The captured dialog has no semantic Shell items view.",
            )
        shell_control = self.automation.ControlFromHandle(shell_view)
        items = shell_control.ListControl(searchDepth=2, Name="Items View")
        if not items.Exists(0.25, 0.02):
            raise WindowsFileDialogError(
                "items_view_missing",
                "The captured dialog did not expose its semantic Items View.",
            )
        self._require_control_in_context(context, items)
        return items

    @staticmethod
    def _shell_display_name(path: str) -> str:
        info = _SHFILEINFOW()
        result = ctypes.windll.shell32.SHGetFileInfoW(
            str(path),
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
            _SHGFI_DISPLAYNAME,
        )
        value = str(info.szDisplayName or "").strip()
        if not result or not value:
            raise WindowsFileDialogError(
                "selection_item_missing",
                "Windows Shell could not resolve the target's semantic display identity.",
            )
        return value

    def select_file_item(
        self,
        context: WindowsFileDialogContext,
        target_path: str,
        filename: str,
        *,
        deadline: float,
        cancelled: threading.Event | None,
    ) -> str:
        if cancelled is not None and cancelled.is_set():
            raise WindowsFileDialogError("cancelled", "Dialog action was cancelled before file selection.")
        self._require_context_current(context)
        self._items_view(context)
        display_name = self._shell_display_name(target_path)
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            raise WindowsFileDialogError("selection_timeout", "The Open action timed out before semantic selection.")

        item = None
        search_deadline = time.monotonic() + min(1.5, remaining)
        while time.monotonic() < search_deadline:
            if cancelled is not None and cancelled.is_set():
                raise WindowsFileDialogError("cancelled", "Dialog action was cancelled while resolving the file row.")
            self._require_context_current(context)
            fresh_items = self._items_view(context)
            matches = []
            try:
                for candidate in fresh_items.GetChildren():
                    if (
                        str(candidate.ControlTypeName or "") == "ListItemControl"
                        and str(candidate.Name or "") == display_name
                    ):
                        matches.append(candidate)
            except Exception:
                matches = []
            if len(matches) == 1:
                item = matches[0]
                break
            if len(matches) > 1:
                raise WindowsFileDialogError(
                    "selection_identity_ambiguous",
                    "The Open dialog exposes multiple rows with the target's Shell display name.",
                )
            time.sleep(0.025)
        if item is None:
            raise WindowsFileDialogError(
                "selection_item_missing",
                f"The Open dialog does not expose the requested file in its current Items View: {filename}",
            )
        self._require_control_in_context(context, item)
        if cancelled is not None and cancelled.is_set():
            raise WindowsFileDialogError("cancelled", "Dialog action was cancelled before semantic file selection.")
        self._require_context_current(context)
        item.GetSelectionItemPattern().Select()
        if time.monotonic() >= deadline:
            raise WindowsFileDialogError(
                "selection_timeout_after_change",
                "The Open action exceeded its timeout after changing the semantic file selection; it was not replayed.",
                ambiguous=True,
            )
        self._require_context_current(context)

        # Reacquire everything after the UIA side effect.  No Control/Pattern is
        # retained as proof across a semantic generation change.
        fresh_items = self._items_view(context)
        fresh_matches = []
        try:
            for candidate in fresh_items.GetChildren():
                if (
                    str(candidate.ControlTypeName or "") == "ListItemControl"
                    and str(candidate.Name or "") == display_name
                ):
                    fresh_matches.append(candidate)
        except Exception:
            fresh_matches = []
        if len(fresh_matches) != 1:
            raise WindowsFileDialogError(
                "selection_readback_mismatch",
                "The requested file disappeared from the Open dialog after selection.",
            )
        fresh_item = fresh_matches[0]
        self._require_control_in_context(context, fresh_item)
        if not bool(fresh_item.GetSelectionItemPattern().IsSelected):
            raise WindowsFileDialogError(
                "selection_readback_mismatch",
                "The Open dialog did not retain the requested file as selected.",
            )
        try:
            selected = fresh_items.GetSelectionPattern().GetSelection()
            selected_names = tuple(str(control.Name or "") for control in selected)
        except Exception as exc:
            raise WindowsFileDialogError(
                "selection_readback_mismatch",
                "The Open dialog selection could not be read back semantically.",
            ) from exc
        if selected_names != (display_name,):
            raise WindowsFileDialogError(
                "selection_readback_mismatch",
                "The Open dialog semantic selection does not match the requested file exactly.",
            )
        return str(filename)

    def set_file_name(self, context: WindowsFileDialogContext, filename: str) -> str:
        self._require_context_current(context)
        edit = self._find_descendant(
            context.hwnd,
            control_id=_SAVE_FILE_NAME_CONTROL_ID,
            class_name="Edit",
        )
        if not edit:
            raise WindowsFileDialogError(
                "filename_control_missing",
                "The captured dialog has no exact native File name edit.",
            )
        edit_control = self.automation.ControlFromHandle(edit)
        edit_runtime_id = self._require_control_in_context(context, edit_control)
        edit_control_handle = int(getattr(edit_control, "NativeWindowHandle", 0) or 0)
        automation_id = str(getattr(edit_control, "AutomationId", "") or "")
        if (
            edit_control_handle != edit
            or str(getattr(edit_control, "ControlTypeName", "") or "") != "EditControl"
            or str(getattr(edit_control, "Name", "") or "").strip() != _FILE_NAME_LABEL
            or str(self.win32gui.GetClassName(edit) or "") != "Edit"
            or automation_id != str(_SAVE_FILE_NAME_CONTROL_ID)
            or int(self.win32gui.GetDlgCtrlID(edit)) != _SAVE_FILE_NAME_CONTROL_ID
        ):
            raise WindowsFileDialogError(
                "filename_control_missing",
                "The captured dialog File name edit has no supported semantic/native identity.",
            )
        thread_id, process_id = self.win32process.GetWindowThreadProcessId(edit)
        del thread_id
        if (
            int(process_id) != context.process_id
            or int(self.win32gui.GetAncestor(edit, self.win32con.GA_ROOT) or 0) != context.hwnd
        ):
            raise WindowsFileDialogError("stale_context", "The File name edit belongs to a different dialog.")
        self._require_context_current(context)
        # Real Common Item Dialog hosts can expose a visually updated ValuePattern
        # without accepting that value as their eventual Save filename. Bind the
        # existing exact native edit, focus it, and drive the normal text-input
        # path instead. This remains non-accepting: Open/Save is never invoked.
        edit_control.SetFocus()
        focused = self.automation.GetFocusedControl()
        self._require_control_in_context(
            context,
            focused,
            expected_runtime_id=edit_runtime_id,
        )
        if int(getattr(focused, "NativeWindowHandle", 0) or 0) != edit:
            raise WindowsFileDialogError(
                "focus_changed",
                "The File name edit lost exact native focus before filename input.",
            )
        self._require_context_current(context)
        focused.SendKeys("{Ctrl}a{Delete}", waitTime=0.02)
        if filename:
            focused.SendKeys(_send_keys_literal(str(filename)), waitTime=0.02, charMode=True)
        self._require_context_current(context)
        fresh_edit = self._find_descendant(
            context.hwnd,
            control_id=_SAVE_FILE_NAME_CONTROL_ID,
            class_name="Edit",
        )
        if fresh_edit != edit:
            raise WindowsFileDialogError(
                "filename_control_missing",
                "The captured dialog File name edit changed native identity.",
            )
        fresh_control = self.automation.ControlFromHandle(fresh_edit)
        self._require_control_in_context(context, fresh_control)
        if int(getattr(fresh_control, "NativeWindowHandle", 0) or 0) != edit:
            raise WindowsFileDialogError("filename_control_missing", "The captured dialog File name edit changed native identity.")
        value = str(fresh_control.GetValuePattern().Value or "")
        if value != str(filename):
            raise WindowsFileDialogError("filename_write_mismatch", "The File name edit did not retain the exact selected filename.")
        return value

    def read_file_name(self, context: WindowsFileDialogContext) -> str:
        self._require_context_current(context)
        edit = self._find_descendant(
            context.hwnd,
            control_id=_SAVE_FILE_NAME_CONTROL_ID,
            class_name="Edit",
        )
        if not edit:
            raise WindowsFileDialogError(
                "filename_control_missing",
                "The captured dialog has no exact native File name edit.",
            )
        control = self.automation.ControlFromHandle(edit)
        self._require_control_in_context(context, control)
        if (
            int(getattr(control, "NativeWindowHandle", 0) or 0) != edit
            or str(getattr(control, "ControlTypeName", "") or "") != "EditControl"
            or str(getattr(control, "AutomationId", "") or "") != str(_SAVE_FILE_NAME_CONTROL_ID)
            or str(self.win32gui.GetClassName(edit) or "") != "Edit"
            or int(self.win32gui.GetDlgCtrlID(edit)) != _SAVE_FILE_NAME_CONTROL_ID
        ):
            raise WindowsFileDialogError(
                "filename_control_missing",
                "The captured dialog File name edit has no supported semantic/native identity.",
            )
        return str(control.GetValuePattern().Value or "")

    def accept_open_result(
        self,
        context: WindowsFileDialogContext,
        *,
        deadline: float,
        cancelled: threading.Event | None,
    ) -> None:
        """Invoke the exact verified Open button once and prove modal completion."""

        if context.dialog_kind != "open":
            raise WindowsFileDialogError(
                "acceptance_unsupported",
                "Only a captured Open dialog can be committed through this semantic path.",
            )
        if cancelled is not None and cancelled.is_set():
            raise WindowsFileDialogError(
                "cancelled",
                "Dialog action was cancelled before Open acceptance.",
            )
        self._require_context_current(context)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WindowsFileDialogError(
                "acceptance_timeout",
                "The Open action timed out before dialog acceptance.",
            )
        if not self.make_foreground(context.hwnd, timeout_seconds=min(0.25, remaining)):
            raise WindowsFileDialogError(
                "focus_changed",
                "The captured Open dialog could not be foregrounded before acceptance.",
            )
        self._require_context_current(context)
        if self.foreground_window() != context.hwnd:
            raise WindowsFileDialogError(
                "focus_changed",
                "The captured Open dialog did not retain foreground before acceptance.",
            )

        button_hwnd = int(self.win32gui.GetDlgItem(int(context.hwnd), 1) or 0)
        if (
            not button_hwnd
            or str(self.win32gui.GetClassName(button_hwnd) or "") != "Button"
            or str(self.win32gui.GetWindowText(button_hwnd) or "").replace("&", "").strip().casefold() != "open"
        ):
            raise WindowsFileDialogError(
                "accept_control_missing",
                "The captured Open dialog has no supported semantic Open button.",
            )
        button = self.automation.ControlFromHandle(button_hwnd)
        button_runtime_id = self._require_control_in_context(context, button)
        if (
            int(getattr(button, "NativeWindowHandle", 0) or 0) != button_hwnd
            or str(getattr(button, "ControlTypeName", "") or "") != "ButtonControl"
        ):
            raise WindowsFileDialogError(
                "accept_control_missing",
                "The captured Open dialog button identity could not be verified.",
            )
        try:
            if not bool(button.IsEnabled):
                raise WindowsFileDialogError(
                    "accept_control_disabled",
                    "The captured Open dialog button is disabled.",
                )
        except WindowsFileDialogError:
            raise
        except Exception as exc:
            raise WindowsFileDialogError(
                "accept_control_missing",
                "The captured Open dialog button state could not be read.",
            ) from exc

        if cancelled is not None and cancelled.is_set():
            raise WindowsFileDialogError(
                "cancelled",
                "Dialog action was cancelled before Open acceptance.",
            )
        self._require_context_current(context)
        if self.foreground_window() != context.hwnd:
            raise WindowsFileDialogError(
                "focus_changed",
                "The captured Open dialog lost focus before acceptance.",
            )
        fresh_button = self.automation.ControlFromHandle(button_hwnd)
        self._require_control_in_context(
            context,
            fresh_button,
            expected_runtime_id=button_runtime_id,
        )
        if int(getattr(fresh_button, "NativeWindowHandle", 0) or 0) != button_hwnd:
            raise WindowsFileDialogError(
                "focus_changed",
                "The Open button changed native identity before acceptance.",
            )
        try:
            fresh_button.GetInvokePattern().Invoke()
        except Exception as exc:
            raise WindowsFileDialogError(
                "accept_invoke_failed",
                "The native Open invocation returned an error; delivery may have occurred and was not replayed.",
                ambiguous=True,
                acceptance_may_have_occurred=True,
            ) from exc

        closing_transition_started: float | None = None
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled.is_set():
                raise WindowsFileDialogError(
                    "cancelled_after_accept",
                    "Open acceptance was cancelled after invocation; it was not replayed.",
                    ambiguous=True,
                    acceptance_may_have_occurred=True,
                )
            if not self.win32gui.IsWindow(context.hwnd):
                if (
                    context.owner_hwnd
                    and self.win32gui.IsWindow(context.owner_hwnd)
                    and self.win32gui.IsWindowEnabled(context.owner_hwnd)
                ):
                    return
                time.sleep(0.02)
                continue
            descriptor = self.describe(context.hwnd)
            exact_identity = descriptor is not None and _descriptor_matches_context(descriptor, context)
            current_checker = getattr(self, "context_is_current", None)
            still_current = (
                exact_identity
                and (bool(current_checker(context)) if callable(current_checker) else True)
            )
            if still_current:
                closing_transition_started = None
            else:
                now = time.monotonic()
                if closing_transition_started is None:
                    closing_transition_started = now
                elif now - closing_transition_started > 0.75:
                    raise WindowsFileDialogError(
                        "context_changed_after_accept",
                        "The Open dialog identity stopped being current without completing its close transition; it was not replayed.",
                        ambiguous=True,
                        acceptance_may_have_occurred=True,
                    )
            time.sleep(0.02)
        raise WindowsFileDialogError(
            "acceptance_timeout",
            "The Open dialog did not confirm completion before the timeout; it was not replayed.",
            ambiguous=True,
            acceptance_may_have_occurred=True,
        )

    def accept_save_result(
        self,
        context: WindowsFileDialogContext,
        *,
        target_path: str,
        filename: str,
        deadline: float,
        cancelled: threading.Event | None,
    ) -> bool:
        """Invoke exact Save once, handle exact overwrite confirmation, and prove modal completion."""

        if context.dialog_kind != "save":
            raise WindowsFileDialogError(
                "acceptance_unsupported",
                "Only a captured Save dialog can be committed through this semantic path.",
            )
        if cancelled is not None and cancelled.is_set():
            raise WindowsFileDialogError("cancelled", "Dialog action was cancelled before Save acceptance.")
        self._require_context_current(context)
        if self.read_file_name(context) != str(filename):
            raise WindowsFileDialogError(
                "readback_mismatch",
                "The Save dialog filename changed before acceptance.",
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WindowsFileDialogError("acceptance_timeout", "The Save action timed out before dialog acceptance.")
        if not self.make_foreground(context.hwnd, timeout_seconds=min(0.25, remaining)):
            raise WindowsFileDialogError(
                "focus_changed",
                "The captured Save dialog could not be foregrounded before acceptance.",
            )
        self._require_context_current(context)
        if self.foreground_window() != context.hwnd:
            raise WindowsFileDialogError(
                "focus_changed",
                "The captured Save dialog did not retain foreground before acceptance.",
            )

        button_hwnd = int(self.win32gui.GetDlgItem(int(context.hwnd), 1) or 0)
        if (
            not button_hwnd
            or str(self.win32gui.GetClassName(button_hwnd) or "") != "Button"
            or str(self.win32gui.GetWindowText(button_hwnd) or "").replace("&", "").strip().casefold() != "save"
        ):
            raise WindowsFileDialogError(
                "accept_control_missing",
                "The captured Save dialog has no supported semantic Save button.",
            )
        button = self.automation.ControlFromHandle(button_hwnd)
        button_runtime_id = self._require_control_in_context(context, button)
        if (
            int(getattr(button, "NativeWindowHandle", 0) or 0) != button_hwnd
            or str(getattr(button, "ControlTypeName", "") or "") != "ButtonControl"
            or not bool(button.IsEnabled)
        ):
            raise WindowsFileDialogError(
                "accept_control_missing",
                "The captured Save dialog button identity/state could not be verified.",
            )
        if cancelled is not None and cancelled.is_set():
            raise WindowsFileDialogError("cancelled", "Dialog action was cancelled before Save acceptance.")
        self._require_context_current(context)
        fresh_button = self.automation.ControlFromHandle(button_hwnd)
        self._require_control_in_context(
            context,
            fresh_button,
            expected_runtime_id=button_runtime_id,
        )
        if self.read_file_name(context) != str(filename):
            raise WindowsFileDialogError(
                "readback_mismatch",
                "The Save dialog filename changed immediately before acceptance.",
            )
        try:
            fresh_button.GetInvokePattern().Invoke()
        except Exception as exc:
            raise WindowsFileDialogError(
                "accept_invoke_failed",
                "The native Save invocation returned an error; delivery may have occurred and was not replayed.",
                ambiguous=True,
                acceptance_may_have_occurred=True,
            ) from exc

        overwrite_confirmed = False
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled.is_set():
                raise WindowsFileDialogError(
                    "cancelled_after_accept",
                    "Save acceptance was cancelled after dispatch; it was not replayed.",
                    ambiguous=True,
                    acceptance_may_have_occurred=True,
                )
            if not self.win32gui.IsWindow(context.hwnd):
                if (
                    context.owner_hwnd
                    and self.win32gui.IsWindow(context.owner_hwnd)
                    and self.win32gui.IsWindowEnabled(context.owner_hwnd)
                ):
                    return overwrite_confirmed
                time.sleep(0.02)
                continue

            popup = int(self.win32gui.GetWindow(context.hwnd, self.win32con.GW_ENABLEDPOPUP) or 0)
            if popup and popup != context.hwnd:
                try:
                    thread_id, process_id = self.win32process.GetWindowThreadProcessId(popup)
                    if not self.win32gui.IsWindowVisible(popup):
                        # The shell may publish GW_ENABLEDPOPUP one message pump
                        # before the overwrite confirmation becomes visible.
                        # Do not misclassify that creation window as a different
                        # modal; wait for the same bounded delivery deadline, then
                        # apply the full identity/control checks once it is visible.
                        time.sleep(0.02)
                        continue
                    if (
                        str(self.win32gui.GetClassName(popup) or "") != _WINDOWS_DIALOG_CLASS
                        or int(self.win32gui.GetWindow(popup, self.win32con.GW_OWNER) or 0) != context.hwnd
                        or int(thread_id) != context.thread_id
                        or int(process_id) != context.process_id
                    ):
                        raise WindowsFileDialogError(
                            "unexpected_save_popup",
                            "A different modal appeared after Save dispatch; the Save was not replayed.",
                            ambiguous=True,
                            acceptance_may_have_occurred=True,
                        )
                    root = self.automation.ControlFromHandle(popup)
                    controls: list[Any] = []
                    pending = list(root.GetChildren())
                    while pending:
                        control = pending.pop(0)
                        controls.append(control)
                        try:
                            pending.extend(control.GetChildren())
                        except Exception:
                            pass
                    text_names = tuple(
                        str(control.Name or "")
                        for control in controls
                        if str(getattr(control, "ControlTypeName", "") or "") == "TextControl"
                    )
                    if not any(str(filename).casefold() in text.casefold() for text in text_names):
                        raise WindowsFileDialogError(
                            "unexpected_save_popup",
                            "The overwrite confirmation does not name the exact requested file.",
                            ambiguous=True,
                            acceptance_may_have_occurred=True,
                        )
                    yes_buttons = [
                        control
                        for control in controls
                        if (
                            str(getattr(control, "ControlTypeName", "") or "") == "ButtonControl"
                            and str(getattr(control, "Name", "") or "").strip().casefold() == "yes"
                            and str(getattr(control, "AutomationId", "") or "") == "CommandButton_6"
                            and bool(getattr(control, "IsEnabled", False))
                        )
                    ]
                    if len(yes_buttons) != 1:
                        raise WindowsFileDialogError(
                            "overwrite_control_missing",
                            "The exact overwrite confirmation Yes button could not be verified.",
                            ambiguous=True,
                            acceptance_may_have_occurred=True,
                        )
                    yes = yes_buttons[0]
                    yes_hwnd = int(getattr(yes, "NativeWindowHandle", 0) or 0)
                    if (
                        not yes_hwnd
                        or int(getattr(yes, "ProcessId", 0) or 0) != context.process_id
                        or str(self.win32gui.GetClassName(yes_hwnd) or "") not in {"Button", "CCPushButton"}
                        or int(self.win32gui.GetAncestor(yes_hwnd, self.win32con.GA_ROOT) or 0) != popup
                    ):
                        raise WindowsFileDialogError(
                            "overwrite_control_missing",
                            "The overwrite confirmation button identity could not be verified.",
                            ambiguous=True,
                            acceptance_may_have_occurred=True,
                        )
                    # Matching a filename/control establishes what the prompt asks;
                    # it does not authorize replacing an existing file. Keep the
                    # native confirmation visible for the human to decide. Save
                    # was already invoked once, so the outcome is ambiguous until
                    # the host/destination can be inspected; never auto-replay it.
                    raise WindowsFileDialogError(
                        "overwrite_requires_confirmation",
                        "Windows requests confirmation to replace the exact selected file. "
                        "No automatic Yes or retry was attempted; decide in the native Save prompt.",
                        ambiguous=True,
                        acceptance_may_have_occurred=True,
                    )
                except WindowsFileDialogError:
                    raise
                except Exception as exc:
                    raise WindowsFileDialogError(
                        "overwrite_control_missing",
                        "The overwrite confirmation could not be verified after Save dispatch.",
                        ambiguous=True,
                        acceptance_may_have_occurred=True,
                    ) from exc
            time.sleep(0.02)

        raise WindowsFileDialogError(
            "save_delivery_timeout",
            "The Save dialog did not confirm completion before the timeout; the Save was not replayed.",
            ambiguous=True,
            acceptance_may_have_occurred=True,
        )


class WindowsFileDialogAdapter:
    """Capture/revalidate one native dialog and perform semantic actions."""

    def __init__(self, *, backend: Any | None = None, default_timeout_seconds: float = 8.0) -> None:
        self._backend = backend or _WindowsUiAutomationBackend()
        self.default_timeout_seconds = max(0.25, min(float(default_timeout_seconds), 10.0))
        self._last_token = ""
        self._last_generation = 0
        self._action_lock = threading.Lock()

    @staticmethod
    def _token(descriptor: _DialogDescriptor) -> str:
        runtime = ".".join(str(value) for value in descriptor.runtime_id)
        return (
            f"windows-dialog:{descriptor.process_id}:{descriptor.process_start_time_ns}:{descriptor.thread_id}:"
            f"{descriptor.hwnd}:{descriptor.owner_hwnd}:{descriptor.dialog_kind}:{runtime}"
        )

    def _automation_context(self):
        factory = getattr(self._backend, "automation_context", None)
        return factory() if callable(factory) else nullcontext()

    def capture_foreground(self) -> WindowsFileDialogContext | None:
        if self._action_lock.locked():
            return None
        with self._automation_context():
            foreground_before = int(self._backend.foreground_window() or 0)
            descriptor = self._backend.describe(foreground_before)
            foreground_after = int(self._backend.foreground_window() or 0)
        if descriptor is not None and (
            not foreground_before
            or foreground_before != foreground_after
            or descriptor.hwnd != foreground_before
        ):
            descriptor = None
        if descriptor is None:
            self._last_generation += 1
            self._last_token = ""
            return None
        token = self._token(descriptor)
        if token == self._last_token and self._last_generation > 0:
            generation = self._last_generation
        else:
            generation = self._last_generation + 1
            self._last_generation = generation
            self._last_token = token
        context = WindowsFileDialogContext(
            schema="listary_windows_file_context_v1",
            context_token=token,
            generation=generation,
            hwnd=descriptor.hwnd,
            process_id=descriptor.process_id,
            thread_id=descriptor.thread_id,
            owner_hwnd=descriptor.owner_hwnd,
            runtime_id=descriptor.runtime_id,
            window_class=descriptor.window_class,
            title=descriptor.title,
            dialog_kind=descriptor.dialog_kind,
            current_directory=descriptor.current_directory,
            process_start_time_ns=descriptor.process_start_time_ns,
        )
        current_checker = getattr(self._backend, "context_is_current", None)
        if callable(current_checker) and not bool(current_checker(context)):
            self._last_generation += 1
            self._last_token = ""
            return None
        return context

    def _revalidate(self, context: WindowsFileDialogContext) -> _DialogDescriptor:
        if context.generation != self._last_generation or context.context_token != self._last_token:
            raise WindowsFileDialogError("stale_context", "The captured Open/Save dialog context is no longer current.")
        descriptor = self._backend.describe(context.hwnd)
        if descriptor is None:
            raise WindowsFileDialogError("stale_context", "The captured Open/Save dialog is no longer available.")
        if not _descriptor_matches_context(descriptor, context) or self._token(descriptor) != context.context_token:
            raise WindowsFileDialogError("stale_context", "The captured Open/Save dialog was replaced or changed identity.")
        current_checker = getattr(self._backend, "context_is_current", None)
        if callable(current_checker) and not bool(current_checker(context)):
            raise WindowsFileDialogError("stale_context", "The captured Open/Save dialog is no longer the owner's active modal.")
        return descriptor

    def prepare_context_for_action(
        self,
        context: WindowsFileDialogContext,
        *,
        timeout_seconds: float = 0.35,
    ) -> None:
        """Re-prove/refocus the captured modal on its UI-owner thread.

        Windows may reject ``SetForegroundWindow`` from a QThreadPool worker even
        when this process owns the foreground Listary window.  The production Qt
        caller invokes this short, non-mutating handoff on its GUI thread before
        dispatching the worker.  The worker then revalidates the same context,
        query/result generation and canonical item again before any dialog state
        changes, so this does not weaken the final action fence.
        """

        if self._action_lock.locked():
            raise WindowsFileDialogError(
                "action_in_progress",
                "A previous Open/Save action is still resolving for this captured dialog.",
            )
        timeout = max(0.05, min(float(timeout_seconds), 1.0))
        with self._automation_context():
            self._revalidate(context)
            if not self._backend.make_foreground(context.hwnd, timeout_seconds=timeout):
                raise WindowsFileDialogError(
                    "focus_changed",
                    "The captured Open/Save dialog could not be foregrounded safely.",
                )
            self._revalidate(context)
            foreground = getattr(self._backend, "foreground_window", None)
            if callable(foreground) and int(foreground() or 0) != context.hwnd:
                raise WindowsFileDialogError(
                    "focus_changed",
                    "The captured Open/Save dialog did not remain foreground after handoff.",
                )

    @staticmethod
    def failure_receipt(
        context: WindowsFileDialogContext,
        *,
        code: str,
        detail: str,
        target_path: str,
        item_kind: str,
        item_id: int,
        root_id: str,
        stable_file_id: str,
        displayed_path: str,
        query_text: str,
        activation_generation: int,
        action_generation: int,
        result_generation: int,
        accept_open: bool = False,
        accept_save: bool = False,
        ambiguous: bool = False,
        acceptance_may_have_occurred: bool = False,
        action_latency_ms: float = 0.0,
    ) -> WindowsFileDialogFailureReceipt:
        """Build one non-authorizing receipt for a failure before native mutation."""

        kind = str(item_kind)
        action = (
            "open"
            if bool(accept_open) and kind == "file" and context.dialog_kind == "open"
            else "save" if bool(accept_save) and kind in {"file", "folder"} and context.dialog_kind == "save"
            else "navigate" if kind == "folder" else (
                "prepare" if context.dialog_kind == "save" else "select"
            )
        )
        target = os.path.abspath(os.path.expanduser(str(target_path)))
        return WindowsFileDialogFailureReceipt(
            schema="listary_windows_file_dialog_failure_v1",
            outcome="failed",
            code=str(code or "file_dialog_error"),
            action=action,
            dialog_kind=context.dialog_kind,
            context_token=context.context_token,
            context_generation=context.generation,
            activation_generation=max(0, int(activation_generation)),
            query_text=str(query_text),
            item_id=int(item_id),
            root_id=str(root_id or ""),
            stable_file_id=str(stable_file_id or ""),
            displayed_path=str(displayed_path),
            target_path=target,
            action_latency_ms=max(0.0, float(action_latency_ms)),
            accepted_by_dialog=False,
            ambiguous=bool(ambiguous),
            detail=str(detail),
            action_generation=max(0, int(action_generation)),
            result_generation=max(0, int(result_generation)),
            item_kind=kind,
            acceptance_may_have_occurred=bool(acceptance_may_have_occurred),
            dispatch_happened=bool(acceptance_may_have_occurred),
        )

    def apply_result(
        self,
        context: WindowsFileDialogContext,
        *,
        target_path: str,
        item_kind: str,
        item_id: int,
        root_id: str,
        stable_file_id: str,
        displayed_path: str,
        query_text: str,
        activation_generation: int,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
        revalidate_target: Callable[[], None] | None = None,
        accept_open: bool = False,
        accept_save: bool = False,
        action_generation: int = 0,
        result_generation: int | None = None,
    ) -> WindowsFileDialogReceipt:
        if not self._action_lock.acquire(blocking=False):
            raise WindowsFileDialogError(
                "action_in_progress",
                "Another Open/Save Quick Switch action is already using this dialog.",
            )
        started = time.perf_counter()
        action = (
            "open"
            if bool(accept_open) and str(item_kind) == "file" and context.dialog_kind == "open"
            else "save" if bool(accept_save) and str(item_kind) in {"file", "folder"} and context.dialog_kind == "save"
            else "navigate" if str(item_kind) == "folder" else "select"
        )
        if str(item_kind) == "file" and context.dialog_kind == "save" and not bool(accept_save):
            action = "prepare"
        target = os.path.abspath(os.path.expanduser(str(target_path)))
        resolved_result_generation = (
            max(0, int(activation_generation))
            if result_generation is None
            else max(0, int(result_generation))
        )
        try:
            with self._automation_context():
                return self._apply_result_initialized(
                    context,
                    target_path=target,
                    item_kind=item_kind,
                    item_id=item_id,
                    root_id=root_id,
                    stable_file_id=stable_file_id,
                    displayed_path=displayed_path,
                    query_text=query_text,
                    activation_generation=activation_generation,
                    cancel_event=cancel_event,
                    timeout_seconds=timeout_seconds,
                    revalidate_target=revalidate_target,
                    accept_open=bool(accept_open),
                    accept_save=bool(accept_save),
                    action_generation=max(0, int(action_generation)),
                    result_generation=resolved_result_generation,
                )
        except WindowsFileDialogError as exc:
            if exc.receipt is None:
                exc.receipt = WindowsFileDialogFailureReceipt(
                    schema="listary_windows_file_dialog_failure_v1",
                    outcome="failed",
                    code=exc.code,
                    action=action,
                    dialog_kind=context.dialog_kind,
                    context_token=context.context_token,
                    context_generation=context.generation,
                    activation_generation=max(0, int(activation_generation)),
                    query_text=str(query_text),
                    item_id=int(item_id),
                    root_id=str(root_id or ""),
                    stable_file_id=str(stable_file_id or ""),
                    displayed_path=str(displayed_path),
                    target_path=target,
                    action_latency_ms=(time.perf_counter() - started) * 1000.0,
                    accepted_by_dialog=False,
                    ambiguous=exc.ambiguous,
                    detail=str(exc),
                    action_generation=max(0, int(action_generation)),
                    result_generation=resolved_result_generation,
                    item_kind=str(item_kind),
                    acceptance_may_have_occurred=exc.acceptance_may_have_occurred,
                    dispatch_happened=bool(exc.acceptance_may_have_occurred),
                )
            raise
        finally:
            self._action_lock.release()

    def _apply_result_initialized(
        self,
        context: WindowsFileDialogContext,
        *,
        target_path: str,
        item_kind: str,
        item_id: int,
        root_id: str,
        stable_file_id: str,
        displayed_path: str,
        query_text: str,
        activation_generation: int,
        cancel_event: threading.Event | None,
        timeout_seconds: float | None,
        revalidate_target: Callable[[], None] | None,
        accept_open: bool,
        accept_save: bool,
        action_generation: int,
        result_generation: int,
    ) -> WindowsFileDialogReceipt:
        started = time.perf_counter()
        timeout = self.default_timeout_seconds if timeout_seconds is None else max(0.25, min(float(timeout_seconds), 10.0))
        deadline = time.monotonic() + timeout
        action = (
            "open"
            if bool(accept_open) and str(item_kind) == "file" and context.dialog_kind == "open"
            else "save" if bool(accept_save) and str(item_kind) in {"file", "folder"} and context.dialog_kind == "save"
            else "navigate" if str(item_kind) == "folder" else (
                "prepare" if context.dialog_kind == "save" else "select"
            )
        )
        target = os.path.abspath(os.path.expanduser(str(target_path)))
        if str(item_kind) not in {"file", "folder"}:
            raise WindowsFileDialogError("unsupported_result_kind", "Open/Save Quick Switch accepts only indexed files and folders.")
        _probe_target(target, expect_directory=str(item_kind) == "folder")
        descriptor = self._revalidate(context)
        before = descriptor.current_directory
        if cancel_event is not None and cancel_event.is_set():
            raise WindowsFileDialogError("cancelled", "Dialog action was cancelled before any native change.")

        selected_after: str | None = None
        prepared_filename_after: str | None = None
        accepted_by_dialog = False
        dispatch_happened = False
        saved_path_after: str | None = None
        overwrite_confirmed = False
        artifact_changed = False
        saved_snapshot: _FileSnapshot | None = None
        pre_save_snapshot = (
            _snapshot_file(target)
            if bool(accept_save) and str(item_kind) == "file" and context.dialog_kind == "save"
            else None
        )
        save_destination_path = target if pre_save_snapshot is not None else None
        navigation_dispatched = False
        target_directory = target if str(item_kind) == "folder" else os.path.dirname(target)
        if descriptor.current_directory is None or not _paths_equal(descriptor.current_directory, target_directory):
            if revalidate_target is not None:
                revalidate_target()
            _probe_target(target, expect_directory=str(item_kind) == "folder")
            self._revalidate(context)
            directory_after, navigation_dispatched = self._backend.commit_directory_navigation(
                context,
                target_directory,
                deadline=deadline,
                cancelled=cancel_event,
            )
        else:
            directory_after = descriptor.current_directory
        descriptor = self._revalidate(context)
        current_after = descriptor.current_directory or directory_after
        if current_after is None or not _paths_equal(current_after, target_directory):
            raise WindowsFileDialogError("readback_mismatch", "The dialog folder readback does not match the requested target.")

        if str(item_kind) == "folder" and bool(accept_save):
            if context.dialog_kind != "save":
                raise WindowsFileDialogError(
                    "acceptance_unsupported",
                    "Semantic Save acceptance requires a captured Save dialog.",
                )
            if cancel_event is not None and cancel_event.is_set():
                raise WindowsFileDialogError("cancelled", "The Save action was cancelled before filename preparation.")
            filename = self._backend.read_file_name(context)
            if (
                not filename
                or filename in {".", ".."}
                or os.path.basename(filename) != filename
                or "/" in filename
                or "\\" in filename
            ):
                raise WindowsFileDialogError(
                    "filename_invalid",
                    "The Save dialog does not contain one exact filename that can be committed.",
                )
            prepared_filename_after = self._backend.set_file_name(context, filename)
            if prepared_filename_after != filename:
                raise WindowsFileDialogError(
                    "readback_mismatch",
                    "The Save dialog did not retain the exact prepared filename.",
                )
            descriptor_after_name = self._revalidate(context)
            if (
                descriptor_after_name.current_directory is None
                or not _paths_equal(descriptor_after_name.current_directory, target_directory)
            ):
                raise WindowsFileDialogError(
                    "readback_mismatch",
                    "The Save dialog folder changed while the filename was being prepared.",
                )
            if revalidate_target is not None:
                revalidate_target()
            if cancel_event is not None and cancel_event.is_set():
                raise WindowsFileDialogError("cancelled", "The Save action was cancelled before dialog acceptance.")
            save_destination_path = os.path.join(target_directory, filename)
            if os.path.exists(save_destination_path):
                raise WindowsFileDialogError(
                    "destination_exists",
                    "A file appeared at the requested Save destination. Refresh before overwriting it.",
                )
            self._revalidate(context)
            if self._backend.read_file_name(context) != filename:
                raise WindowsFileDialogError(
                    "readback_mismatch",
                    "The Save dialog filename changed immediately before dispatch.",
                )
            if os.path.exists(save_destination_path):
                raise WindowsFileDialogError(
                    "destination_exists",
                    "A file appeared at the requested Save destination immediately before dispatch.",
                )
            try:
                overwrite_confirmed = bool(
                    self._backend.accept_save_result(
                        context,
                        target_path=save_destination_path,
                        filename=filename,
                        deadline=deadline,
                        cancelled=cancel_event,
                    )
                )
                dispatch_happened = True
            except WindowsFileDialogError as exc:
                if exc.acceptance_may_have_occurred:
                    dispatch_happened = True
                raise
            last_snapshot_error: WindowsFileDialogError | None = None
            while time.monotonic() < deadline:
                try:
                    candidate = _snapshot_file(save_destination_path)
                except WindowsFileDialogError as exc:
                    last_snapshot_error = exc
                    if exc.code == "target_changed":
                        raise WindowsFileDialogError(
                            "save_verification_unstable",
                            "The new Save destination changed during verification; Save was not replayed.",
                            ambiguous=True,
                            acceptance_may_have_occurred=True,
                        ) from exc
                    time.sleep(0.02)
                    continue
                saved_snapshot = _verified_saved_snapshot(save_destination_path, candidate)
                artifact_changed = True
                saved_path_after = save_destination_path
                break
            if not artifact_changed or saved_snapshot is None:
                detail = "The Save dialog closed, but the new destination artifact could not be verified."
                if last_snapshot_error is not None:
                    detail += f" Last artifact readback: {last_snapshot_error}"
                raise WindowsFileDialogError(
                    "save_delivery_unproven",
                    detail,
                    ambiguous=True,
                    acceptance_may_have_occurred=True,
                )
            if overwrite_confirmed:
                raise WindowsFileDialogError(
                    "unexpected_overwrite",
                    "A new-file Save unexpectedly required overwrite confirmation; the Save was not replayed.",
                    ambiguous=True,
                    acceptance_may_have_occurred=True,
                )
            accepted_by_dialog = True

        if str(item_kind) == "file":
            if cancel_event is not None and cancel_event.is_set():
                raise WindowsFileDialogError("cancelled", "Dialog action was cancelled before file selection.")
            if time.monotonic() >= deadline:
                raise WindowsFileDialogError(
                    "selection_timeout",
                    "The Open/Save action timed out before filename selection; it was not replayed.",
                    ambiguous=navigation_dispatched,
                )
            if revalidate_target is not None:
                revalidate_target()
            if time.monotonic() >= deadline:
                raise WindowsFileDialogError(
                    "selection_timeout",
                    "The Open/Save action timed out during final result revalidation; filename selection was not attempted.",
                    ambiguous=navigation_dispatched,
                )
            _probe_target(target, expect_directory=False)
            self._revalidate(context)
            filename = os.path.basename(target)
            if not filename:
                raise WindowsFileDialogError("target_invalid", "The selected file has no filename component.")
            if context.dialog_kind == "open":
                selected_name = self._backend.select_file_item(
                    context,
                    target,
                    filename,
                    deadline=deadline,
                    cancelled=cancel_event,
                )
                readback = selected_name
            else:
                readback = self._backend.set_file_name(context, filename)
                prepared_filename_after = readback
                if time.monotonic() >= deadline:
                    raise WindowsFileDialogError(
                        "selection_timeout_after_write",
                        "The Save preparation exceeded its timeout after writing the filename; no retry was attempted.",
                        ambiguous=True,
                    )
            descriptor_after_file = self._revalidate(context)
            directory_after_file = descriptor_after_file.current_directory
            if directory_after_file is None or not _paths_equal(directory_after_file, target_directory):
                raise WindowsFileDialogError(
                    "readback_mismatch",
                    "The dialog folder changed while the selected filename was being written.",
                )
            current_after = directory_after_file
            if readback != filename:
                raise WindowsFileDialogError(
                    "readback_mismatch",
                    "The dialog semantic file state does not match the requested file.",
                )
            if context.dialog_kind == "open":
                selected_after = os.path.join(current_after, readback)
                if not _paths_equal(selected_after, target):
                    raise WindowsFileDialogError(
                        "readback_mismatch",
                        "The Open dialog semantic selection resolves to a different file.",
                    )
            if revalidate_target is not None:
                revalidate_target()
            _probe_target(target, expect_directory=False)
            self._revalidate(context)
            if bool(accept_open):
                if context.dialog_kind != "open":
                    raise WindowsFileDialogError(
                        "acceptance_unsupported",
                        "Semantic dialog acceptance is currently supported only for Open-file results.",
                    )
                if cancel_event is not None and cancel_event.is_set():
                    raise WindowsFileDialogError(
                        "cancelled",
                        "The Open action was cancelled before dialog acceptance.",
                    )
                if revalidate_target is not None:
                    revalidate_target()
                if cancel_event is not None and cancel_event.is_set():
                    raise WindowsFileDialogError(
                        "cancelled",
                        "The Open action was cancelled before dialog acceptance.",
                    )
                _probe_target(target, expect_directory=False)
                self._revalidate(context)
                self._backend.accept_open_result(
                    context,
                    deadline=deadline,
                    cancelled=cancel_event,
                )
                dispatch_happened = True
                accepted_by_dialog = True
            elif bool(accept_save):
                if context.dialog_kind != "save":
                    raise WindowsFileDialogError(
                        "acceptance_unsupported",
                        "Semantic Save acceptance requires a captured Save dialog.",
                    )
                if cancel_event is not None and cancel_event.is_set():
                    raise WindowsFileDialogError("cancelled", "The Save action was cancelled before dialog acceptance.")
                if revalidate_target is not None:
                    revalidate_target()
                if cancel_event is not None and cancel_event.is_set():
                    raise WindowsFileDialogError("cancelled", "The Save action was cancelled before dialog acceptance.")
                _probe_target(target, expect_directory=False)
                immediate_snapshot = _snapshot_file(target)
                if pre_save_snapshot is None:
                    raise WindowsFileDialogError("target_unavailable", "The Save destination has no pre-dispatch snapshot.")
                if _snapshot_changed(pre_save_snapshot, immediate_snapshot):
                    raise WindowsFileDialogError(
                        "target_changed",
                        "The Save destination changed after selection and before Save dispatch.",
                    )
                self._revalidate(context)
                if self._backend.read_file_name(context) != filename:
                    raise WindowsFileDialogError(
                        "readback_mismatch",
                        "The Save dialog filename changed immediately before dispatch.",
                    )
                try:
                    overwrite_confirmed = bool(
                        self._backend.accept_save_result(
                            context,
                            target_path=target,
                            filename=filename,
                            deadline=deadline,
                            cancelled=cancel_event,
                        )
                    )
                    dispatch_happened = True
                except WindowsFileDialogError as exc:
                    if exc.acceptance_may_have_occurred:
                        dispatch_happened = True
                    raise

                last_snapshot_error: WindowsFileDialogError | None = None
                while time.monotonic() < deadline:
                    try:
                        candidate = _snapshot_file(target)
                    except WindowsFileDialogError as exc:
                        last_snapshot_error = exc
                        if exc.code == "target_changed":
                            raise WindowsFileDialogError(
                                "save_verification_unstable",
                                "The Save destination changed during verification; Save was not replayed.",
                                ambiguous=True,
                                acceptance_may_have_occurred=True,
                            ) from exc
                        time.sleep(0.02)
                        continue
                    if _snapshot_changed(pre_save_snapshot, candidate):
                        saved_snapshot = _verified_saved_snapshot(target, candidate)
                        artifact_changed = True
                        saved_path_after = target
                        break
                    time.sleep(0.02)
                if not artifact_changed or saved_snapshot is None:
                    detail = "The Save dialog closed, but the destination artifact did not provide proof of a delivered write."
                    if last_snapshot_error is not None:
                        detail += f" Last artifact readback: {last_snapshot_error}"
                    raise WindowsFileDialogError(
                        "save_delivery_unproven",
                        detail,
                        ambiguous=True,
                        acceptance_may_have_occurred=True,
                    )
                accepted_by_dialog = True

        return WindowsFileDialogReceipt(
            schema="listary_windows_file_dialog_receipt_v1",
            outcome="verified",
            action=action,
            dialog_kind=context.dialog_kind,
            context_token=context.context_token,
            context_generation=context.generation,
            activation_generation=max(0, int(activation_generation)),
            query_text=str(query_text),
            item_id=int(item_id),
            root_id=str(root_id or ""),
            stable_file_id=str(stable_file_id or ""),
            displayed_path=str(displayed_path),
            target_path=saved_path_after if action == "save" and saved_path_after is not None else target,
            directory_before=before,
            directory_after=current_after,
            selected_path_after=selected_after,
            action_latency_ms=(time.perf_counter() - started) * 1000.0,
            accepted_by_dialog=accepted_by_dialog,
            ambiguous=False,
            detail=(
                "Exact Open-dialog selection was verified, Open was invoked through the verified UI Automation button, "
                "and dialog closure plus owner re-enable were observed."
                if accepted_by_dialog and action == "open"
                else (
                        "Exact Save-dialog filename was verified, Save was invoked once through the verified UI Automation "
                        "button, any exact overwrite confirmation was handled semantically, and destination artifact change "
                        "plus dialog closure/owner re-enable and a stable destination SHA-256 readback were observed."
                    if accepted_by_dialog and action == "save"
                    else "Folder/file state verified through Windows UI Automation; Open/Save was not invoked."
                )
            ),
            action_generation=max(0, int(action_generation)),
            result_generation=max(0, int(result_generation)),
            item_kind=str(item_kind),
            prepared_filename_after=prepared_filename_after,
            acceptance_may_have_occurred=False,
            dispatch_happened=dispatch_happened,
            saved_path_after=saved_path_after,
            overwrite_confirmed=overwrite_confirmed,
            artifact_changed=artifact_changed,
            saved_size_bytes=saved_snapshot.size_bytes if saved_snapshot is not None else None,
            saved_modified_ns=saved_snapshot.modified_ns if saved_snapshot is not None else None,
            saved_file_id=saved_snapshot.file_id if saved_snapshot is not None else "",
            saved_sha256=saved_snapshot.sha256 if saved_snapshot is not None else "",
            saved_hash_verified=bool(saved_snapshot is not None and len(saved_snapshot.sha256) == 64),
        )


__all__ = [
    "WindowsFileDialogAdapter",
    "WindowsFileDialogContext",
    "WindowsFileDialogError",
    "WindowsFileDialogFailureReceipt",
    "WindowsFileDialogReceipt",
]

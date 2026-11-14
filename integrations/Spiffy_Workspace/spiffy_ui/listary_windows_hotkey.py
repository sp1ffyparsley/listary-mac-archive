"""Windows-only global double-Control activation for the Spiffy Listary shell.

This module owns input activation only. Search/index/ranking remain in the
resident ``listary-agent`` and ``listary-core`` owners.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
from ctypes import wintypes
import os
import psutil
import sys
import threading
import time
from typing import Final

from PySide6.QtCore import QObject, Signal


VK_CONTROL: Final = 0x11
VK_LCONTROL: Final = 0xA2
VK_RCONTROL: Final = 0xA3
CONTROL_KEYS: Final = frozenset((VK_CONTROL, VK_LCONTROL, VK_RCONTROL))


def installed_listary_owns_double_control() -> bool:
    """Never compete with a separate resident original Listary on double-Control.

    The installed reference stays untouched. Our distinct Ctrl+Alt+Space
    fallback and task-owned menu can still open Spiffy Listary explicitly.
    Scan only when the user actually completes a hotkey gesture; no watcher.
    """
    if sys.platform != "win32":
        return False
    try:
        own_pid = os.getpid()
        return any(
            process.info.get("pid") != own_pid
            and str(process.info.get("name") or "").casefold() == "listary.exe"
            for process in psutil.process_iter(attrs=("pid", "name"))
        )
    except (psutil.Error, OSError):
        # Unknown resident ownership must never cause a second launcher to
        # take the original global gesture. The tray and fallback stay usable.
        return True

WM_KEYDOWN: Final = 0x0100
WM_KEYUP: Final = 0x0101
WM_SYSKEYDOWN: Final = 0x0104
WM_SYSKEYUP: Final = 0x0105
WM_QUIT: Final = 0x0012
WM_HOTKEY: Final = 0x0312
WH_KEYBOARD_LL: Final = 13
HC_ACTION: Final = 0
LLKHF_INJECTED: Final = 0x10
MOD_ALT: Final = 0x0001
MOD_CONTROL: Final = 0x0002
MOD_NOREPEAT: Final = 0x4000
VK_SPACE: Final = 0x20
FALLBACK_HOTKEY_ID: Final = 0x4C53


@dataclass(slots=True)
class DoubleControlDetector:
    """Pure state machine for Listary-style double-Control activation."""

    interval_seconds: float = 0.42
    max_hold_seconds: float = 0.65
    _down_at: dict[int, float] = field(default_factory=dict)
    _invalid_down: set[int] = field(default_factory=set)
    _last_tap_up: float | None = None

    def reset(self) -> None:
        self._down_at.clear()
        self._invalid_down.clear()
        self._last_tap_up = None

    def feed(self, vk_code: int, *, is_down: bool, timestamp: float, injected: bool = False) -> bool:
        """Consume one key transition and return ``True`` on a valid double tap."""

        if injected:
            return False

        vk_code = int(vk_code)
        timestamp = float(timestamp)
        if vk_code not in CONTROL_KEYS:
            if is_down:
                self._last_tap_up = None
                self._invalid_down.update(self._down_at)
            return False

        if is_down:
            # Ignore key-repeat. Pressing left and right Control together is a
            # chord, not two taps, so it cancels the pending sequence.
            if vk_code in self._down_at:
                return False
            if self._down_at:
                self._last_tap_up = None
                self._invalid_down.update(self._down_at)
                self._invalid_down.add(vk_code)
            self._down_at[vk_code] = timestamp
            return False

        started = self._down_at.pop(vk_code, None)
        if started is None:
            return False
        if vk_code in self._invalid_down:
            self._invalid_down.discard(vk_code)
            self._last_tap_up = None
            return False
        held_for = max(0.0, timestamp - started)
        if held_for > self.max_hold_seconds:
            self._last_tap_up = None
            return False

        previous = self._last_tap_up
        self._last_tap_up = timestamp
        if previous is None:
            return False
        if 0.0 <= timestamp - previous <= self.interval_seconds:
            # Consume the pair so a triple tap does not fire twice.
            self._last_tap_up = None
            return True
        return False


if sys.platform == "win32":

    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]


    _HOOK_PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)


class WindowsDoubleControlHook(QObject):
    """Low-level Windows keyboard listener that never suppresses key events."""

    activated = Signal()
    fallback_activated = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        interval_seconds: float = 0.42,
        max_hold_seconds: float = 0.65,
    ) -> None:
        super().__init__(parent)
        self.detector = DoubleControlDetector(interval_seconds=interval_seconds, max_hold_seconds=max_hold_seconds)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._thread_id: int | None = None
        self._hook_handle: int | None = None
        self._hook_proc = None
        self._startup_error: BaseException | None = None
        self._fallback_registered = False

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive() and self._hook_handle)

    @property
    def fallback_registered(self) -> bool:
        """Whether Ctrl+Alt+Space was registered as the normal chord fallback."""

        return bool(self._fallback_registered)

    def start(self, *, timeout_seconds: float = 2.0) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows double-Control activation is available only on Windows")
        if self.running:
            return
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Windows double-Control hook is still starting")

        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run_hook, name="SpiffyListaryHotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(max(0.1, float(timeout_seconds))):
            self.stop()
            raise RuntimeError("Windows double-Control hook did not become ready")
        if self._startup_error is not None:
            error = self._startup_error
            self.stop()
            raise RuntimeError(f"Windows double-Control hook could not start: {error}") from error
        if not self.running:
            self.stop()
            raise RuntimeError("Windows double-Control hook exited during startup")

    def stop(self, *, timeout_seconds: float = 2.0) -> None:
        thread = self._thread
        thread_id = self._thread_id
        if thread is None:
            return
        if thread.is_alive() and thread_id and sys.platform == "win32":
            ctypes.windll.user32.PostThreadMessageW(int(thread_id), WM_QUIT, 0, 0)
            thread.join(max(0.1, float(timeout_seconds)))
        if thread.is_alive():
            raise RuntimeError("Windows double-Control hook did not stop cleanly")
        self._thread = None
        self._thread_id = None
        self._hook_handle = None
        self._hook_proc = None
        self._fallback_registered = False
        self.detector.reset()

    def _run_hook(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOK_PROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL

        self._thread_id = int(kernel32.GetCurrentThreadId())

        @_HOOK_PROC
        def hook_proc(n_code: int, w_param: int, l_param: int) -> int:
            if n_code == HC_ACTION:
                event = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                message = int(w_param)
                is_down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
                is_up = message in (WM_KEYUP, WM_SYSKEYUP)
                if is_down or is_up:
                    should_activate = self.detector.feed(
                        int(event.vkCode),
                        is_down=is_down,
                        timestamp=time.monotonic(),
                        injected=bool(int(event.flags) & LLKHF_INJECTED),
                    )
                    if should_activate:
                        self.activated.emit()
            return int(user32.CallNextHookEx(self._hook_handle or 0, n_code, w_param, l_param))

        self._hook_proc = hook_proc
        try:
            handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_proc, None, 0)
            if not handle:
                raise ctypes.WinError()
            self._hook_handle = int(handle)
            self._fallback_registered = bool(
                user32.RegisterHotKey(
                    None,
                    FALLBACK_HOTKEY_ID,
                    MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                    VK_SPACE,
                )
            )
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            return

        self._ready.set()
        message = wintypes.MSG()
        try:
            while True:
                result = int(user32.GetMessageW(ctypes.byref(message), None, 0, 0))
                if result == 0:
                    break
                if result < 0:
                    raise ctypes.WinError()
                if int(message.message) == WM_HOTKEY and int(message.wParam) == FALLBACK_HOTKEY_ID:
                    self.fallback_activated.emit()
                    continue
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if self._fallback_registered:
                user32.UnregisterHotKey(None, FALLBACK_HOTKEY_ID)
                self._fallback_registered = False
            handle = self._hook_handle
            if handle:
                user32.UnhookWindowsHookEx(handle)
            self._hook_handle = None


__all__ = [
    "CONTROL_KEYS",
    "DoubleControlDetector",
    "FALLBACK_HOTKEY_ID",
    "installed_listary_owns_double_control",
    "WindowsDoubleControlHook",
]

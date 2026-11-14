from __future__ import annotations

from spiffy_ui.listary_windows_hotkey import DoubleControlDetector, VK_LCONTROL, VK_RCONTROL


def test_installed_listary_retains_double_control_when_its_process_is_running(monkeypatch) -> None:
    import os
    from types import SimpleNamespace
    from spiffy_ui import listary_windows_hotkey as owner

    monkeypatch.setattr(owner.sys, "platform", "win32")
    monkeypatch.setattr(owner.psutil, "process_iter", lambda attrs: iter([
        SimpleNamespace(info={"pid": os.getpid(), "name": "python.exe"}),
        SimpleNamespace(info={"pid": 456789, "name": "Listary.exe"}),
    ]))
    assert owner.installed_listary_owns_double_control()


def test_installed_listary_double_control_custody_yields_to_no_external_process(monkeypatch) -> None:
    import os
    from types import SimpleNamespace
    from spiffy_ui import listary_windows_hotkey as owner

    monkeypatch.setattr(owner.sys, "platform", "win32")
    monkeypatch.setattr(owner.psutil, "process_iter", lambda attrs: iter([
        SimpleNamespace(info={"pid": os.getpid(), "name": "Listary.exe"}),
        SimpleNamespace(info={"pid": 22, "name": "OpusMac.exe"}),
    ]))
    assert not owner.installed_listary_owns_double_control()


def test_installed_listary_custody_fails_closed_when_process_census_fails(monkeypatch) -> None:
    from spiffy_ui import listary_windows_hotkey as owner

    monkeypatch.setattr(owner.sys, "platform", "win32")
    def unavailable(*, attrs):
        raise owner.psutil.AccessDenied(pid=123)
    monkeypatch.setattr(owner.psutil, "process_iter", unavailable)
    assert owner.installed_listary_owns_double_control()


def test_double_control_and_explicit_fallback_use_distinct_signals() -> None:
    from spiffy_ui.listary_windows_hotkey import WindowsDoubleControlHook

    hook = WindowsDoubleControlHook()
    events: list[str] = []
    hook.activated.connect(lambda: events.append("double-control"))
    hook.fallback_activated.connect(lambda: events.append("explicit-fallback"))
    hook.activated.emit()
    hook.fallback_activated.emit()
    assert events == ["double-control", "explicit-fallback"]


def _tap(detector: DoubleControlDetector, vk_code: int, down: float, up: float) -> bool:
    assert detector.feed(vk_code, is_down=True, timestamp=down) is False
    return detector.feed(vk_code, is_down=False, timestamp=up)


def test_double_control_detector_fires_once_for_two_short_taps() -> None:
    detector = DoubleControlDetector(interval_seconds=0.4, max_hold_seconds=0.6)
    assert _tap(detector, VK_LCONTROL, 1.00, 1.05) is False
    assert _tap(detector, VK_LCONTROL, 1.20, 1.25) is True
    assert _tap(detector, VK_LCONTROL, 1.32, 1.37) is False


def test_double_control_detector_accepts_left_then_right_control() -> None:
    detector = DoubleControlDetector(interval_seconds=0.4)
    assert _tap(detector, VK_LCONTROL, 2.00, 2.04) is False
    assert _tap(detector, VK_RCONTROL, 2.20, 2.24) is True


def test_double_control_detector_rejects_other_key_between_taps() -> None:
    detector = DoubleControlDetector(interval_seconds=0.5)
    assert _tap(detector, VK_LCONTROL, 3.00, 3.05) is False
    assert detector.feed(ord("C"), is_down=True, timestamp=3.10) is False
    assert detector.feed(ord("C"), is_down=False, timestamp=3.12) is False
    assert _tap(detector, VK_LCONTROL, 3.20, 3.25) is False


def test_double_control_detector_does_not_count_control_chord_as_tap() -> None:
    detector = DoubleControlDetector(interval_seconds=0.5)
    assert _tap(detector, VK_LCONTROL, 3.50, 3.55) is False
    assert detector.feed(VK_LCONTROL, is_down=True, timestamp=3.60) is False
    assert detector.feed(ord("C"), is_down=True, timestamp=3.62) is False
    assert detector.feed(ord("C"), is_down=False, timestamp=3.64) is False
    assert detector.feed(VK_LCONTROL, is_down=False, timestamp=3.66) is False
    assert _tap(detector, VK_LCONTROL, 3.80, 3.85) is False


def test_double_control_detector_rejects_long_hold_and_slow_second_tap() -> None:
    detector = DoubleControlDetector(interval_seconds=0.3, max_hold_seconds=0.4)
    assert detector.feed(VK_LCONTROL, is_down=True, timestamp=4.00) is False
    assert detector.feed(VK_LCONTROL, is_down=False, timestamp=4.50) is False
    assert _tap(detector, VK_LCONTROL, 5.00, 5.05) is False
    assert _tap(detector, VK_LCONTROL, 5.50, 5.55) is False


def test_double_control_detector_ignores_injected_and_repeat_events() -> None:
    detector = DoubleControlDetector(interval_seconds=0.4)
    assert detector.feed(VK_LCONTROL, is_down=True, timestamp=6.00, injected=True) is False
    assert detector.feed(VK_LCONTROL, is_down=False, timestamp=6.04, injected=True) is False
    assert detector.feed(VK_LCONTROL, is_down=True, timestamp=6.10) is False
    assert detector.feed(VK_LCONTROL, is_down=True, timestamp=6.12) is False
    assert detector.feed(VK_LCONTROL, is_down=False, timestamp=6.15) is False
    assert _tap(detector, VK_LCONTROL, 6.30, 6.35) is True

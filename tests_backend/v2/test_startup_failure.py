"""The packaged exe has no console, so a failed start must say why."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.backend_v2 import startup_failure
from src.storage_migrator.control import (
    LOCKED_MESSAGE,
    DataRootAlreadyLocked,
    DataRootLock,
)


def test_lock_conflict_message_tells_the_user_what_to_do() -> None:
    error = DataRootAlreadyLocked(LOCKED_MESSAGE)

    # Chinese, actionable, and free of the old "Saber Translator" name.
    assert "MiyakoKB" in str(error)
    assert "任务管理器" in str(error)
    assert "Saber Translator" not in str(error)
    assert "DataRootAlreadyLocked" not in str(error)


def test_a_second_lock_on_the_same_root_is_rejected(tmp_path: Path) -> None:
    first = DataRootLock(tmp_path)
    first.acquire()
    try:
        second = DataRootLock(tmp_path)
        with pytest.raises(DataRootAlreadyLocked):
            second.acquire()
    finally:
        first.release()


def test_the_lock_is_reusable_after_release(tmp_path: Path) -> None:
    with DataRootLock(tmp_path):
        pass
    # The next start after a clean exit must not inherit a stale lock.
    with DataRootLock(tmp_path):
        pass


def test_failure_log_records_the_traceback(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "startup-failure.log"
    error = ValueError("boom")
    try:
        raise error
    except ValueError as caught:
        startup_failure.show_startup_failure(caught, log_path=log_path)

    written = log_path.read_text(encoding="utf-8")
    assert "ValueError: boom" in written
    assert "Traceback (most recent call last)" in written


def test_failure_log_appends_across_restarts(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "startup-failure.log"
    for index in range(2):
        try:
            raise RuntimeError(f"attempt-{index}")
        except RuntimeError as caught:
            startup_failure.show_startup_failure(caught, log_path=log_path)

    written = log_path.read_text(encoding="utf-8")
    assert "attempt-0" in written
    assert "attempt-1" in written


def test_a_long_message_is_replaced_by_a_generic_hint() -> None:
    assert "没" in startup_failure._user_message(RuntimeError("x" * 5000))
    assert startup_failure._user_message(RuntimeError("这一句会原样显示")) == (
        "这一句会原样显示"
    )


def test_reporting_never_masks_the_original_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import saber_v2

    def explode(_error: BaseException) -> None:
        raise OSError("dialog host is gone")

    monkeypatch.setattr(startup_failure, "show_startup_failure", explode)

    # A failure while reporting still yields a plain exit code, not a second
    # traceback escaping into the void.
    assert saber_v2._report_failure(ValueError("root cause")) == 1


def test_a_broken_tk_dialog_falls_through_to_the_native_message_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown: list[str] = []

    def broken(_message: str) -> bool:
        raise RuntimeError("Tcl_Init failed")

    def record(message: str) -> bool:
        shown.append(message)
        return True

    monkeypatch.setattr(startup_failure, "_show_tk_dialog", broken)
    monkeypatch.setattr(startup_failure, "_show_msgbox", record)

    # Losing Tk must not trade a dialog for a silent exit.
    startup_failure._show_dialog("失败原因", None, "traceback")
    assert shown == ["失败原因"]


def test_a_retry_choice_still_reaches_the_retry_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ask_again(_message: str) -> bool:
        raise SystemExit(startup_failure.RETRY_EXIT_CODE)

    monkeypatch.setattr(startup_failure, "_show_tk_dialog", ask_again)

    with pytest.raises(SystemExit):
        startup_failure._show_dialog("失败原因", None, "traceback")

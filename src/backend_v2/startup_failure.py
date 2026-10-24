"""Report a fatal startup error to a user who has no terminal.

The packaged executable is built ``console=False``: a double-click launch has
no console, and PyInstaller's own traceback dialog is disabled, so any exception
that escapes ``main()`` disappears without a trace. Users only ever saw the
browser tab close and then nothing, or a stack trace if they happened to run
the exe from a terminal.

This module renders the same failure as a native dialog plus a written log, and
only falls back to the console when no dialog can be shown (a normal console
Python, CI, or a host without Tk).
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

TITLE = "MiyakoKB 启动失败"

# Anything longer than this is a programming error, not a user-actionable
# message; the dialog shows a generic hint and points at the log file.
_USER_MESSAGE_LIMIT = 600


def show_startup_failure(error: BaseException, *, log_path: Path | None = None) -> None:
    """Surface ``error`` to the user and return; never raises."""
    detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    log_hint = _write_failure_log(detail, log_path)
    message = _user_message(error)

    if _console_attached():
        # A terminal is already showing the traceback; do not stack a dialog on
        # top of it. Writing to that console may itself fail on a legacy code
        # page, which must not cost the user the dialog below.
        try:
            print(f"{TITLE}：{message}", file=sys.stderr)
            if log_hint is not None:
                print(f"详细日志：{log_hint}", file=sys.stderr)
        except (OSError, UnicodeError):
            _show_dialog(message, log_hint, detail)
        return

    _show_dialog(message, log_hint, detail)


def _user_message(error: BaseException) -> str:
    text = str(error).strip()
    if not text or len(text) > _USER_MESSAGE_LIMIT:
        return (
            "MiyakoKB 没能启动，发生了未预期的错误。\n\n"
            "请重试；如果反复出现，请把日志文件发给开发者。"
        )
    return text


def _console_attached() -> bool:
    """True when stderr is a real console, not a devnull placeholder.

    ``restore_standard_streams`` replaces a missing stderr with ``os.devnull``
    for windowed launches, so the stream is never ``None`` by the time we get
    here. Comparing the open file against ``devnull`` is what actually tells a
    double-click launch apart from a terminal launch.
    """
    stream = getattr(sys, "stderr", None)
    if stream is None:
        return False
    if os.name != "nt":
        return stream.isatty()
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except (AttributeError, OSError):
        return stream.isatty()


def _write_failure_log(detail: str, log_path: Path | None) -> Path | None:
    candidates: list[Path] = []
    if log_path is not None:
        candidates.append(log_path)
    else:
        default = _default_log_path()
        if default is not None:
            candidates.append(default)
    # Always try the temp directory too: a frozen launch may resolve a data root
    # that does not exist, and losing the evidence defeats the whole mechanism.
    candidates.append(_fallback_log_path())
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            with candidate.open("a", encoding="utf-8") as handle:
                handle.write(detail)
                if not detail.endswith("\n"):
                    handle.write("\n")
            return candidate
        except OSError:
            continue
    return None


def _default_log_path() -> Path | None:
    try:
        from src.backend_v2.paths import resolve_data_root
        from src.storage_migrator.control import control_root

        return control_root(resolve_data_root(None)) / "logs" / "startup-failure.log"
    except Exception:  # resolving the data root may itself be what failed
        return None


def _fallback_log_path() -> Path:
    """A writable location that does not depend on the data root resolving.

    ``--data-dir`` is a CLI argument, which this reporter does not see, so the
    default path can name a root the launch never used. Writing here as a second
    attempt means a failure always leaves a trace somewhere the user can find.
    """
    import tempfile

    return Path(tempfile.gettempdir()) / "MiyakoKB-startup-failure.log"


def _show_dialog(message: str, log_hint: Path | None, detail: str) -> None:
    body = message
    if log_hint is not None:
        body = f"{body}\n\n详细日志：{log_hint}"
    try:
        if _show_tk_dialog(body):
            return
    except SystemExit:
        raise
    except Exception:
        pass

    # The dialog is the only thing standing between a silent exit and the user,
    # so a Tk failure must not end here.
    try:
        if _show_msgbox(body):
            return
    except Exception:
        pass

    # No UI could be shown. The traceback is already on disk: the caller wrote
    # it before asking for any dialog, so there is nothing left to do here.
    _ = detail


def _show_tk_dialog(message: str) -> bool:
    """Native dialog with a Retry/Quit choice.

    Retry returns to the caller, which exits with a distinct code; the shim in
    ``__main__`` uses that to re-run startup, so a user who closed the other
    instance can retry without hunting for the exe again.
    """
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
    except Exception:  # tkinter excluded from the bundle, or no desktop
        return False
    try:
        root.withdraw()
        # Lift above a frozen launcher that has no visible window of its own.
        root.attributes("-topmost", True)
        retry = messagebox.askretrycancel(TITLE, message, parent=root)
    except Exception:
        return False
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    if retry:
        raise SystemExit(RETRY_EXIT_CODE)
    return True


def _show_msgbox(message: str) -> bool:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, message, TITLE, 0x00000010  # MB_ICONERROR
        )
        return True
    except (AttributeError, OSError):
        return False


RETRY_EXIT_CODE = 3

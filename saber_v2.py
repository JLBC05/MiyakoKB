"""Backend-first v2 role dispatcher. 

This module must stay dependency-light.  Role-specific imports happen only after
``multiprocessing.freeze_support()`` and argument parsing so the API process
never imports Worker-only model, vector, or plugin modules as a side effect.
"""

from __future__ import annotations

import multiprocessing


def main() -> int:
    from src.backend_v2.standard_streams import restore_standard_streams

    restore_standard_streams()
    multiprocessing.freeze_support()

    from src.backend_v2.dispatch import dispatch

    return dispatch()


def _report_failure(error: BaseException) -> int:
    """Turn an escaping startup error into a visible message, not a silent exit.

    The packaged exe is windowed, so an uncaught exception would otherwise
    vanish: the user sees the browser tab closed and nothing else.
    """
    try:
        from src.backend_v2.startup_failure import show_startup_failure

        show_startup_failure(error)
    except BaseException:  # reporting must never mask the original failure
        pass
    return 1


def run() -> int:
    """Run until the backend exits, letting the user retry a failed start."""
    while True:
        try:
            return main()
        except SystemExit as exit_request:
            from src.backend_v2.startup_failure import RETRY_EXIT_CODE

            if exit_request.code != RETRY_EXIT_CODE:
                raise
            continue
        except KeyboardInterrupt:
            return 130
        except BaseException as error:
            return _report_failure(error)


if __name__ == "__main__":
    raise SystemExit(run())

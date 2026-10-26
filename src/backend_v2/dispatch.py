"""Command-line role dispatch for the isolated v2 runtime."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from src.backend_v2.runtime_profile import PROFILE_NAMES
from src.shared.user_logging import user_log


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Saber Translator backend-first v2")
    parser.add_argument(
        "--role",
        choices=("launcher", "api", "worker", "storage-migrator"),
        default="launcher",
        help="Process role.",
    )
    from src.version import APP_VERSION
    parser.add_argument("--version", action="version", version=APP_VERSION)
    parser.add_argument("--action", choices=("check", "upgrade"), default="check")
    parser.add_argument("--data-dir", help="Explicit v2 data root.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument(
        "--profile",
        choices=PROFILE_NAMES,
        default="local",
        help="Fixed runtime policy profile.",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Allow direct API/Worker startup without Launcher-issued epoch identity.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Initialize the selected role, print a JSON smoke result, and exit.",
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--legacy-launcher",
        action="store_true",
        help="Deprecated alias for the supervised launcher (the default).",
    )
    parser.add_argument(
        "--unified-launcher",
        action="store_true",
        help="Run the one-click daemon launcher used by run.py (expects a prepared data root).",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help=(
            "Console and product-file log level (default INFO). Pass DEBUG to keep "
            "full model prompts in the product log; they always land in the "
            "*-diagnostic.log file instead."
        ),
    )
    return parser


def dispatch(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.role == "storage-migrator":
        from src.storage_migrator.entrypoint import run_migrator
        return run_migrator(args)

    if args.role in {"api", "worker"}:
        try:
            if args.role == "api":
                from src.backend_v2.api.entrypoint import run_api as run_child
            else:
                from src.backend_v2.worker.entrypoint import run_worker as run_child
            return run_child(args)
        except Exception as error:
            # Background roles must exit instead of opening a frozen-runtime dialog.
            message = f"{args.role.upper()} 进程启动或运行失败：{error}"
            logging.getLogger(f"saber.{args.role}").exception(message)
            user_log("error", message, level=logging.ERROR)
            return 1

    if args.role == "launcher":
        if getattr(args, "unified_launcher", False):
            # One-click developer entry (also used by run.py): assumes a
            # prepared data root and starts API/Worker in explicit test mode.
            from src.backend_v2.launcher.unified_launcher import run_unified_launcher
            return run_unified_launcher(
                host=args.host,
                port=args.port,
                data_dir=args.data_dir,
                log_level=args.log_level,
                open_browser=not args.no_browser,
            )

        # Default: the supervised launcher owns storage preparation,
        # launcher-issued epochs, and child restarts.
        from src.backend_v2.launcher.entrypoint import run_launcher
        return run_launcher(args)

    from src.backend_v2.launcher.entrypoint import run_launcher

    return run_launcher(args)

"""MiyakoKB / Saber Translator Unified Daemon Launcher Entrypoint.

Starts both API and Worker processes simultaneously and protects their lifecycles.
Usage:
    python run.py
    python run.py --port 5000 --host 127.0.0.1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to sys.path if not present
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.backend_v2.launcher.unified_launcher import (
    UnifiedLauncher,
    run_unified_launcher,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MiyakoKB / Saber Translator Unified Daemon Launcher"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the API server (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port to bind the API server (default: 5000).",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Explicit v2 data directory.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=None,
        help="Console log level.",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Automatically open the browser once the service is ready.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the browser.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    open_browser = args.open_browser and not args.no_browser
    return run_unified_launcher(
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
        log_level=args.log_level,
        open_browser=open_browser,
    )


if __name__ == "__main__":
    raise SystemExit(main())

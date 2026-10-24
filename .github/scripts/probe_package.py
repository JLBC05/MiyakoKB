"""Check each packaged role initializes outside the source checkout."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# The release executable is built windowed (console=False). When its temp
# directory or its working directory lives anywhere under the system temp root,
# Python's tempfile probe cannot prove a candidate writable and even `--version`
# dies with "No usable temporary directory found". A CI runner starts steps with
# the working directory set to a fresh directory under that very root, so the
# probe has to move both the working directory and TEMP/TMP somewhere else.
_TEMP_VARIABLES = ("TEMP", "TMP", "TMPDIR")


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _temp_roots() -> list[Path]:
    seen: list[Path] = []
    for value in (os.environ.get("TEMP"), os.environ.get("TMP"), os.environ.get("TMPDIR"), tempfile.gettempdir()):
        if value and Path(value) not in seen:
            seen.append(Path(value))
    return seen


def _probe_base() -> Path:
    """Return a writable directory that is outside every temp root.

    The sandbox handed to us normally already is (CI passes a runner temp path,
    which sits under the system temp root, so it normally is not) — therefore
    the preferred home is a sibling of the checkout, which the runner always
    owns and which is never a temp directory.
    """
    roots = _temp_roots()
    candidates = [
        Path(__file__).resolve().parents[3] / ".package-probe",
        Path(tempfile.mkdtemp(prefix="saber-package-probe-")),
    ]
    for candidate in candidates:
        resolved = candidate.resolve()
        if any(_path_is_within(resolved, root) for root in roots):
            continue
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved
    raise SystemExit("Cannot find a probe directory outside the system temp root")


def _run(command: list[str], cwd: Path, temp_dir: Path, *, check: bool) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    for name in _TEMP_VARIABLES:
        environment[name] = str(temp_dir)
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=check,
    )


def main() -> None:
    command = [str(Path(argument).resolve()) for argument in sys.argv[1:]]
    if not command:
        raise SystemExit("Usage: probe_package.py EXECUTABLE [ENTRYPOINT]")
    manifest = json.loads((Path(__file__).resolve().parents[2] / "version.json").read_text(encoding="utf-8"))
    base = _probe_base()
    try:
        cwd = base / "cwd"
        temp_dir = base / "temp"
        sandbox = base / "sandbox"
        for directory in (cwd, temp_dir, sandbox):
            directory.mkdir(parents=True, exist_ok=True)

        version = _run([*command, "--version"], cwd, temp_dir, check=True)
        if version.stdout.strip() != manifest["version"]:
            raise RuntimeError("Packaged version differs from release manifest")
        for role in ("launcher", "api", "worker"):
            result = _run(
                [*command, "--role", role, "--probe", "--test-mode",
                 "--data-dir", str(sandbox / role)],
                cwd, temp_dir, check=False,
            )
            print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            result.check_returncode()
            payload = json.loads(result.stdout)
            if payload.get("role") != role or payload.get("status") != "ready":
                raise RuntimeError(f"Unexpected {role} probe result: {payload}")
        for action in ("upgrade", "check"):
            result = _run(
                [*command, "--role", "storage-migrator", "--action", action,
                 "--data-dir", str(sandbox / "storage")],
                cwd, temp_dir, check=True,
            )
            payload = json.loads(result.stdout)
            expected = "created" if action == "upgrade" else "current"
            if payload.get("status") != expected or payload.get("targetVersion") != manifest["storageVersion"]:
                raise RuntimeError(f"Unexpected storage probe result: {payload}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()

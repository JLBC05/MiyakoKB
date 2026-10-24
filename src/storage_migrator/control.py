"""Stable coordination outside the swappable data root; no business DB access."""

from contextlib import contextmanager
from collections.abc import Callable
import json
import os
from pathlib import Path
import stat
import time
import uuid

import psutil

from .contracts import StorageError
from .paths import filesystem_path


def reject_links(path: Path) -> None:
    path = filesystem_path(path)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise StorageError(f"拒绝链接或重解析目录: {path}")


def control_root(root: Path) -> Path:
    root = root.resolve()
    path = root.parent / f".{root.name}.saber-storage"
    reject_links(path)
    return path


def is_empty_root(root: Path) -> bool:
    if not root.exists():
        return True
    for path in root.rglob("*"):
        reject_links(path)
        if path.is_file() and path.relative_to(root).parts[0] not in {"logs", "runtime"}:
            return False
    return True


def atomic_json(path: Path, payload: dict) -> None:
    path = filesystem_path(path)
    reject_links(path.parent)
    reject_links(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    reject_links(temporary)
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class DataRootAlreadyLocked(StorageError):
    pass


LOCKED_MESSAGE = (
    "这个数据目录正被另一个 MiyakoKB 实例占用。\n\n"
    "常见原因：上一个 MiyakoKB 窗口被关掉了，但它在后台的核心进程"
    "（MiyakoKB.exe）还在运行——关掉浏览器页面并不会让它退出。\n\n"
    "请先在任务管理器的「详细信息」里结束所有 MiyakoKB.exe，然后重新启动。"
)

# The lock only needs to be exclusive against *other* launchers. Windows child
# processes normally do not inherit the handle, but an inheritable handle would
# let the API/Worker keep the data root locked after the launcher itself exits,
# which surfaces as a confusing "already locked" error on the next start.
HANDLE_FLAG_INHERIT = 0x00000001


def _harden_lock_handle(handle) -> None:
    if os.name != "nt":
        return
    try:
        import msvcrt
        from ctypes import wintypes, windll

        handle_flags = windll.kernel32.SetHandleInformation
        handle_flags.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
        handle_flags.restype = wintypes.BOOL
        handle_flags(
            msvcrt.get_osfhandle(handle.fileno()),
            wintypes.DWORD(HANDLE_FLAG_INHERIT),
            wintypes.DWORD(0),
        )
    except (OSError, AttributeError, ValueError):
        # Best effort: a failed call leaves the historical behaviour intact.
        return


class DataRootLock:
    def __init__(self, data_root: Path):
        self.data_root = data_root.resolve()
        self._handle = None

    @property
    def lock_path(self) -> Path:
        return control_root(self.data_root) / "launcher.lock"

    def acquire(self) -> None:
        if self._handle is not None:
            raise RuntimeError("data-root lock is already acquired by this object")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        reject_links(self.lock_path)
        handle = self.lock_path.open("a+b")
        try:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise DataRootAlreadyLocked(LOCKED_MESSAGE) from exc
        _harden_lock_handle(handle)
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


def wait_for_children(
    root: Path, *, timeout: float = 5.0,
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    folder = control_root(root) / "processes"
    reject_links(folder)
    if not folder.exists():
        return
    deadline = time.monotonic() + timeout
    while True:
        if check_cancelled is not None:
            check_cancelled()
        live = []
        for path in folder.glob("*.json"):
            reject_links(path)
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("data_root") != str(root.resolve()):
                raise StorageError("进程记录与数据目录不匹配")
            try:
                process = psutil.Process(record["pid"])
                alive = process.create_time() == record["created_at"] and process.is_running()
            except psutil.NoSuchProcess:
                alive = False
            except psutil.AccessDenied as exc:
                raise StorageError(
                    f"无法确认旧 API/Worker 是否退出（PID {record['pid']}，进程信息访问受限）；请先检查旧程序"
                ) from exc
            if alive:
                live.append(record["pid"])
            else:
                path.unlink(missing_ok=True)
        if not live:
            return
        if time.monotonic() >= deadline:
            raise StorageError(f"旧 API/Worker 尚未退出: {live}；请先关闭旧程序")
        time.sleep(0.1)


@contextmanager
def registered_process(root: Path, role: str):
    folder = control_root(root) / "processes"
    reject_links(folder)
    folder.mkdir(parents=True, exist_ok=True)
    process = psutil.Process()
    path = folder / f"{role}-{process.pid}.json"
    atomic_json(path, {"data_root": str(root.resolve()), "pid": process.pid, "created_at": process.create_time(), "role": role})
    # Keep the record until the OS process has exited, including log teardown.
    # The next owner removes records whose PID + creation time are no longer live.
    yield


def business_ready(root: Path) -> bool:
    control = control_root(root)
    pending = control / "upgrade-pending.json"
    if not pending.exists():
        return True
    try:
        operation_id = json.loads(pending.read_text(encoding="utf-8"))["id"]
        if str(uuid.UUID(operation_id)) != operation_id:
            return False
        state = json.loads((control / "operations" / operation_id / "state.json").read_text(encoding="utf-8"))
        return (state.get("id") == operation_id and state.get("root") == str(root.resolve())
                and state.get("stage") in {"committed", "cleaned"})
    except (OSError, ValueError, KeyError, TypeError):
        return False

"""Unified daemon launcher for MiyakoKB / Saber Translator v2.

Manages both API and Worker processes simultaneously with clean lifecycle
supervision, Windows Job Object isolation, and signal / ConsoleCtrlHandler hooks.
"""

from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from typing import Sequence

import psutil

from src.backend_v2.launcher.windows_job import ChildProcessJob
from src.backend_v2.logging_config import LOG_LEVEL_ENV
from src.backend_v2.paths import ensure_data_root, project_root, resolve_data_root
from src.backend_v2.storage.lifecycle import initialize_database
from src.storage_migrator.runner import StorageManager

LOGGER = logging.getLogger("saber.unified_launcher")

BANNER = r"""
======================================================================
  __  __ _             _          _  ______  
 |  \/  (_)           | |        | |/ /  _ \ 
 | \  / |_ _   _  __ _| | _____  | ' /| |_) |
 | |\/| | | | | |/ _` | |/ / _ \ |  < |  _ < 
 | |  | | | |_| | (_| |   < (_) || . \| |_) |
 |_|  |_|_|\__, |\__,_|_|\_\___/ |_|\_\____/ 
            __/ |                            
           |___/                             
                 MiyakoKB 统一守护启动器 (v2)
======================================================================
"""


class UnifiedLauncher:
    """Daemon supervisor managing API and Worker child processes."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 5000,
        data_dir: Path | str | None = None,
        log_level: str | None = None,
        open_browser: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.data_dir = (
            resolve_data_root(data_dir) if data_dir else None
        )
        self.log_level = log_level
        self.open_browser = open_browser

        self.root_dir = project_root()
        self.api_process: subprocess.Popen[str] | None = None
        self.worker_process: subprocess.Popen[str] | None = None
        self.job: ChildProcessJob | None = None
        self.storage_manager: StorageManager | None = None

        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._worker_restarts: list[float] = []

        try:
            self.job = ChildProcessJob()
        except Exception as exc:
            LOGGER.debug("ChildProcessJob initialization bypassed: %s", exc)

    def _prepare_storage(self) -> None:
        """Admit the data root before starting children.

        The one-click launcher starts API/Worker in explicit test mode, so the
        locked conversion step that normally guards child startup has to run
        here; otherwise a fresh data root has no schema at all.

        The manager stays open (lock still held) until :meth:`_confirm_storage`
        runs after the API reports healthy: a storage upgrade only counts as
        committed once the backend actually came up on the new schema, and
        ``upgrade-pending.json`` keeps every business endpoint answered with
        503 until then.
        """
        data_root = ensure_data_root(resolve_data_root(self.data_dir))
        profile = "local"
        manager = StorageManager(data_root, profile)
        manager.__enter__()
        try:
            result = manager.prepare(initialize_database)
            manager.before_start()
        except Exception:
            manager.__exit__(None, None, None)
            raise
        self.storage_manager = manager
        LOGGER.debug("统一启动器存储准备完成：%s", result.get("status"))

    def _confirm_storage(self) -> None:
        """Commit the storage upgrade once the API is healthy, then release the lock."""
        manager = self.storage_manager
        if manager is None:
            return
        try:
            manager.confirm_ready()
        finally:
            manager.__exit__(None, None, None)
            self.storage_manager = None

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["LAUNCHER_PID"] = str(os.getpid())
        if self.log_level:
            # 变量名必须与 logging_config.LOG_LEVEL_ENV 一致，否则 --log-level
            # 只影响启动器自己的控制台，子进程仍按默认 INFO 走。
            env[LOG_LEVEL_ENV] = self.log_level
        return env

    def _spawn_api(self) -> subprocess.Popen[str]:
        entrypoint = str(self.root_dir / "saber_v2.py")
        cmd = [
            sys.executable,
            entrypoint,
            "--role",
            "api",
            "--test-mode",
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        if self.data_dir:
            cmd.extend(["--data-dir", str(self.data_dir)])
        if self.log_level:
            cmd.extend(["--log-level", self.log_level])

        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.root_dir),
            env=self._build_env(),
            creationflags=creation_flags,
        )
        if self.job:
            try:
                self.job.assign(proc)
            except Exception as exc:
                LOGGER.debug("Failed to assign API process to job: %s", exc)
        return proc

    def _spawn_worker(self) -> subprocess.Popen[str]:
        entrypoint = str(self.root_dir / "saber_v2.py")
        cmd = [
            sys.executable,
            entrypoint,
            "--role",
            "worker",
            "--test-mode",
        ]
        if self.data_dir:
            cmd.extend(["--data-dir", str(self.data_dir)])
        if self.log_level:
            cmd.extend(["--log-level", self.log_level])

        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.root_dir),
            env=self._build_env(),
            creationflags=creation_flags,
        )
        if self.job:
            try:
                self.job.assign(proc)
            except Exception as exc:
                LOGGER.debug("Failed to assign Worker process to job: %s", exc)
        return proc

    def _terminate_proc(self, proc: subprocess.Popen[str] | None, name: str, timeout: float = 3.0) -> None:
        if proc is None or proc.poll() is not None:
            return

        pid = proc.pid
        children: list[psutil.Process] = []
        try:
            p = psutil.Process(pid)
            children = p.children(recursive=True)
        except Exception:
            pass

        try:
            proc.terminate()
        except Exception:
            pass
        for c in children:
            try:
                c.terminate()
            except Exception:
                pass

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"[守护清理] {name} (PID: {pid}) 未及时退出，正在强制结束...")
            try:
                proc.kill()
            except Exception:
                pass
            for c in children:
                try:
                    c.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass

    def stop(self) -> None:
        """Stop both managed child processes cleanly."""
        if self._stopping.is_set():
            return
        self._stopping.set()

        print("\n[守护启动器] 正在优雅关闭服务，请稍候...")
        with self._lock:
            # Terminate API first so no new requests are accepted
            if self.api_process is not None:
                self._terminate_proc(self.api_process, "API 服务")
                self.api_process = None

            # Terminate Worker
            if self.worker_process is not None:
                self._terminate_proc(self.worker_process, "Worker 进程")
                self.worker_process = None

        self._abandon_storage_upgrade()
        print("[守护启动器] 所有服务子进程已干净退出。")

    def _abandon_storage_upgrade(self) -> None:
        """Roll back an uncommitted storage upgrade before releasing the lock.

        Children are already gone at this point: a backend that never became
        healthy must not leave the root in the ``starting`` stage, otherwise the
        next launch keeps answering every business endpoint with 503.
        """
        manager = self.storage_manager
        if manager is None:
            return
        try:
            manager.startup_failed(include_installed=True)
        except Exception as exc:
            LOGGER.debug("存储升级回滚失败：%s", exc)
        finally:
            manager.__exit__(None, None, None)
            self.storage_manager = None

    def _poll_health(self) -> None:
        """Background health checker to notify user when ready and optionally open browser."""
        url = f"http://{self.host}:{self.port}/api/v2/health"
        deadline = time.monotonic() + 30.0
        opened = False
        while time.monotonic() < deadline and not self._stopping.is_set():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MiyakoKB-Launcher"})
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        if not self._commit_storage_upgrade():
                            return
                        print(f"\n[服务就绪] Web UI 地址: http://{self.host}:{self.port}")
                        print("[服务就绪] API 与 Worker 协同正常运行。按 Ctrl+C 可停止。\n")
                        if self.open_browser and not opened:
                            opened = True
                            webbrowser.open(f"http://{self.host}:{self.port}")
                        return
            except Exception:
                pass
            time.sleep(0.5)
        if not self._stopping.is_set() and self.storage_manager is not None:
            print("[告警] 服务在 30 秒内未报告就绪，正在回滚存储升级并关闭。")
            self.stop()

    def _commit_storage_upgrade(self) -> bool:
        """Report the storage upgrade as committed now that the API answered healthy.

        Returns ``False`` when the commit failed; the supervisor must then shut
        the children down instead of running on a half-admitted data root.
        """
        try:
            self._confirm_storage()
        except Exception as exc:
            print(f"[错误] 存储升级提交失败: {exc}")
            self.stop()
            return False
        return True

    def run(self) -> int:
        """Main supervisor loop."""
        print(BANNER)
        print(f"  [Web UI 访问] http://{self.host}:{self.port}")
        print("  [启动模式]    统一双进程守护模式 (API + Worker)")
        print("----------------------------------------------------------------------")
        print("  提示: 按 Ctrl+C 退出全部服务")
        print("======================================================================\n")

        try:
            self._prepare_storage()
        except Exception as exc:
            print(f"[错误] 存储准备失败: {exc}")
            return 1

        # Register cleanup hooks
        atexit.register(self.stop)

        def _sig_handler(signum: int, _frame: object) -> None:
            self.stop()

        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _sig_handler)

        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

                def _console_ctrl_handler(dwCtrlType: int) -> bool:
                    self.stop()
                    return False

                self._ctrl_handler = PHANDLER_ROUTINE(_console_ctrl_handler)
                ctypes.windll.kernel32.SetConsoleCtrlHandler(self._ctrl_handler, True)
            except Exception as exc:
                LOGGER.debug("ConsoleCtrlHandler registration bypassed: %s", exc)

        with self._lock:
            try:
                self.api_process = self._spawn_api()
                print(f"[API 进程]   已启动 (PID: {self.api_process.pid})")
            except Exception as exc:
                print(f"[错误] API 进程启动失败: {exc}")
                self.stop()
                return 1

            try:
                self.worker_process = self._spawn_worker()
                print(f"[Worker 进程] 已启动 (PID: {self.worker_process.pid})")
            except Exception as exc:
                print(f"[错误] Worker 进程启动失败: {exc}")
                self.stop()
                return 1

        # Start health check notifier in background
        health_thread = threading.Thread(target=self._poll_health, daemon=True)
        health_thread.start()

        # Supervision loop
        exit_code = 0
        try:
            while not self._stopping.is_set():
                # Check API process
                with self._lock:
                    if self.api_process is not None:
                        api_status = self.api_process.poll()
                        if api_status is not None:
                            print(f"\n[告警] API 进程退出 (退出码: {api_status})，正在关闭系统...")
                            exit_code = api_status
                            break

                    # Check Worker process
                    if self.worker_process is not None:
                        worker_status = self.worker_process.poll()
                        if worker_status is not None:
                            if not self._stopping.is_set():
                                # Check restart rate limit: max 5 restarts in 30 seconds
                                now = time.monotonic()
                                self._worker_restarts = [
                                    t for t in self._worker_restarts if now - t < 30.0
                                ]
                                if len(self._worker_restarts) >= 5:
                                    print(f"\n[严重错误] Worker 进程在 30 秒内连续退出超过 5 次，停止守护。")
                                    exit_code = worker_status or 1
                                    break

                                self._worker_restarts.append(now)
                                print(
                                    f"[告警] Worker 进程异常退出 (退出码: {worker_status})，第 {len(self._worker_restarts)} 次自动重启..."
                                )
                                time.sleep(1.0)
                                if not self._stopping.is_set():
                                    try:
                                        self.worker_process = self._spawn_worker()
                                        print(
                                            f"[Worker 进程] 已重新拉起 (PID: {self.worker_process.pid})"
                                        )
                                    except Exception as exc:
                                        print(f"[错误] Worker 重新拉起失败: {exc}")
                                        exit_code = 1
                                        break

                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

        return exit_code


def run_unified_launcher(
    *,
    host: str = "127.0.0.1",
    port: int = 5000,
    data_dir: Path | str | None = None,
    log_level: str | None = None,
    open_browser: bool = False,
) -> int:
    launcher = UnifiedLauncher(
        host=host,
        port=port,
        data_dir=data_dir,
        log_level=log_level,
        open_browser=open_browser,
    )
    return launcher.run()

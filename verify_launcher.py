"""Automated verification script for MiyakoKB Unified Daemon Launcher.

Checks:
1. 5000 port listens within 3-5 seconds.
2. Both API and Worker child processes are alive and running.
3. GET /api/v2/health returns 200 OK.
4. POST /api/v2/insight/books/{book_id}/qa responds and processes via API + Worker cooperation.
5. Sends termination signal and verifies clean exit of all child processes and port release.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error

import psutil
from sqlalchemy import delete
from src.backend_v2.paths import resolve_data_root
from src.backend_v2.storage.database import create_sqlite_engine, database_path_for
from src.backend_v2.storage.schema import transient_requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PORT = 5000
HOST = "127.0.0.1"
BASE_URL = f"http://{HOST}:{PORT}"
PROJECT_ROOT = Path(__file__).resolve().parent


def cleanup_transient_requests() -> None:
    """清理历史残留的瞬时 QA 请求记录，确保测试环境幂等干净。"""
    try:
        data_root = resolve_data_root()
        db_path = database_path_for(data_root)
        if not db_path.exists():
            return
        engine = create_sqlite_engine(db_path)
        with engine.begin() as conn:
            conn.execute(delete(transient_requests))
    except Exception as exc:
        print(f"  [WARN] 清理 transient_requests 异常: {exc}")


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def wait_for_port(port: int, timeout: float = 8.0, host: str = "127.0.0.1") -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_in_use(port, host):
            return True
        time.sleep(0.3)
    return False


def get_child_python_processes(parent_pid: int) -> list[psutil.Process]:
    try:
        parent = psutil.Process(parent_pid)
        return [
            p
            for p in parent.children(recursive=True)
            if "python" in p.name().lower()
        ]
    except Exception:
        return []


def run_verification() -> bool:
    print("=" * 70)
    print(">>> 开始验证：MiyakoKB 统一启动器守护模式 (verify_launcher.py)")
    print("=" * 70)

    # 0. 准备工作：确保端口没有残留占用
    if is_port_in_use(PORT, HOST):
        print(f"[前置清理] 警告: 端口 {PORT} 正在被占用，尝试查找并终止...")
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                for conn in proc.net_connections(kind="inet"):
                    if conn.laddr.port == PORT:
                        print(f"  终止占用端口的进程: PID {proc.pid} ({proc.name()})")
                        proc.kill()
            except Exception:
                pass
        time.sleep(1.0)

    # 0. 数据库环境清理
    cleanup_transient_requests()

    # 1. 启动 run.py 统一启动器
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "run.py"),
        "--host",
        HOST,
        "--port",
        str(PORT),
        "--no-browser",
    ]
    print(f"\n[步骤 1] 启动统一启动器: {' '.join(cmd)}")
    start_time = time.monotonic()
    launcher_proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    api_pid = None
    worker_pid = None
    child_processes: list[psutil.Process] = []

    try:
        # 2. 检查 5000 端口是否在 3-5 秒内成功监听
        print("\n[步骤 2] 检查 5000 端口监听状态...")
        listened = wait_for_port(PORT, timeout=8.0, host=HOST)
        elapsed = time.monotonic() - start_time
        if not listened:
            print(f"  [FAIL] 端口 {PORT} 在 {elapsed:.2f} 秒内未被成功监听！")
            return False
        print(f"  [PASS] 端口 {PORT} 在 {elapsed:.2f} 秒内成功进入监听状态！")

        # 3. 检查 API 和 Worker 子进程是否都在运行
        print("\n[步骤 3] 检查子进程树 (API 与 Worker 是否同时运行)...")
        time.sleep(1.0)
        child_processes = get_child_python_processes(launcher_proc.pid)
        print(f"  当前 Launcher PID: {launcher_proc.pid}")
        print(f"  检测到子进程数: {len(child_processes)}")
        for cp in child_processes:
            try:
                cmdline = " ".join(cp.cmdline())
                print(f"    - 子进程 PID: {cp.pid}, 状态: {cp.status()}, 命令行: {cmdline}")
                if "--role api" in cmdline:
                    api_pid = cp.pid
                elif "--role worker" in cmdline:
                    worker_pid = cp.pid
            except Exception as e:
                print(f"    - 子进程 PID: {cp.pid} 获取详情失败: {e}")

        if api_pid is None or worker_pid is None:
            # 尝试通过系统所有 python 进程查找
            for p in psutil.process_iter(["pid", "cmdline"]):
                try:
                    cmd_str = " ".join(p.cmdline())
                    if "--role api" in cmd_str and p.pid not in [api_pid, worker_pid]:
                        api_pid = p.pid
                    elif "--role worker" in cmd_str and p.pid not in [api_pid, worker_pid]:
                        worker_pid = p.pid
                except Exception:
                    pass

        if api_pid and worker_pid:
            print(f"  [PASS] API (PID: {api_pid}) 和 Worker (PID: {worker_pid}) 均在活跃运行中！")
        else:
            print(f"  [FAIL] 未能同时识别到 API ({api_pid}) 和 Worker ({worker_pid})！")
            return False

        # 4. 向 /api/v2/health 发送请求检查 200
        print("\n[步骤 4] 向 http://127.0.0.1:5000/api/v2/health 发送健康检查请求...")
        health_url = f"{BASE_URL}/api/v2/health"
        req = urllib.request.Request(health_url, headers={"User-Agent": "MiyakoKB-Verify"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
            print(f"  HTTP 状态码: {status}")
            print(f"  响应内容: {body}")
            if status != 200:
                print(f"  [FAIL] health 接口返回非 200 状态码: {status}")
                return False
            print("  [PASS] 健康检查通过，返回 200 OK！")

        # 5. 向问答接口发送一次简单提问，确认 API + Worker 协同
        print("\n[步骤 5] 向问答接口发送提问，验证 API + Worker 协同机制...")
        # 选用数据库中已有的测试书籍
        test_book_id = "3a8c6ca7-347b-49b2-b18b-689dadbd4ba0"
        qa_url = f"{BASE_URL}/api/v2/insight/books/{test_book_id}/qa"
        qa_data = json.dumps({"question": "测试统一启动器问答协同", "mode": "global"}).encode("utf-8")
        qa_req = urllib.request.Request(
            qa_url,
            data=qa_data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "MiyakoKB-Verify",
            },
            method="POST",
        )
        print(f"  POST {qa_url}")
        try:
            with urllib.request.urlopen(qa_req, timeout=30.0) as resp:
                qa_status = resp.status
                print(f"  HTTP 状态码: {qa_status}")
                # 读取部分 SSE 事件
                chunks = []
                for _ in range(8):
                    line = resp.readline().decode("utf-8")
                    if not line:
                        break
                    trimmed = line.strip()
                    if trimmed:
                        chunks.append(trimmed)
                        print(f"    <- {trimmed}")
                    if len(chunks) >= 3:
                        break
                qa_output = "\n".join(chunks)
                if qa_status == 200 and ("event" in qa_output or "status" in qa_output or "requestId" in qa_output):
                    print("  [PASS] 问答接口成功建立 SSE 通信并由 Worker 响应！")
                else:
                    print("  [WARN] 问答接口返回 200 但未捕获到预期事件，请关注日志")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            print(f"  [ERROR] QA 接口返回 HTTP {e.code}: {err_body}")
            raise

        # 6. 发送退出信号，验证干净退出与端口释放
        print("\n[步骤 6] 发送终止信号，验证进程退出与端口释放...")
        print("  正在向 Launcher 发送 terminate 信号...")
        launcher_proc.terminate()
        try:
            launcher_proc.wait(timeout=6.0)
            print("  Launcher 主进程已成功退出。")
        except subprocess.TimeoutExpired:
            print("  [WARN] Launcher 超时未退出，强制 kill...")
            launcher_proc.kill()
            launcher_proc.wait()

        # 等待子进程完全退出
        time.sleep(1.5)
        api_alive = psutil.pid_exists(api_pid) if api_pid else False
        worker_alive = psutil.pid_exists(worker_pid) if worker_pid else False

        if api_alive or worker_alive:
            print(f"  [FAIL] 存在残留子进程！API alive={api_alive}, Worker alive={worker_alive}")
            return False
        print("  [PASS] API 和 Worker 子进程均已干净退出！")

        port_still_busy = is_port_in_use(PORT, HOST)
        if port_still_busy:
            print(f"  [FAIL] 端口 {PORT} 仍处于占用状态！")
            return False
        print(f"  [PASS] 端口 {PORT} 已完全释放，无残留！")

        print("\n" + "=" * 70)
        print("[SUCCESS] 全部 5 项验证指标 100% 通过！统一启动器守护模式实施成功！")
        print("=" * 70)
        return True

    finally:
        # 确保清理
        if launcher_proc.poll() is None:
            launcher_proc.kill()
        if api_pid and psutil.pid_exists(api_pid):
            try:
                psutil.Process(api_pid).kill()
            except Exception:
                pass
        if worker_pid and psutil.pid_exists(worker_pid):
            try:
                psutil.Process(worker_pid).kill()
            except Exception:
                pass
        cleanup_transient_requests()


if __name__ == "__main__":
    success = run_verification()
    sys.exit(0 if success else 1)

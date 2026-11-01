"""MiyakoKB 分析流水线 stage 性能剖析（只读度量脚本，不进 CI）。

用途
----
U-6① 性能调研：读一条（或全部）已完成的分析 run 里各 stage 的时间戳，
统计每个 stage 的耗时分布（P50 / P95），为后续「L0/L1 批量化、对齐连通
分量并行」的优化决策提供**实测依据**——不要凭直觉改算法。

数据来源
--------
- `jobs`          : 一次 run / derived_rebuild 的 `started_at` / `finished_at`。
- `job_steps.kind`: stage 归属（如 `insight_stage_entity_alignment`、
  `insight_analyze_batch` 等）。
- `job_events`    : `step_started` 事件的 `created_at` + `payload_json.stepKind`，
  用于还原各 stage 的起止时间。

口径（务必理解，避免误读）
--------------------------
- **stage 跨度**：同一 job 内某 stage 的「最后一次 step_started − 第一次
  step_started」。它反映该 stage 在时间轴上的墙钟占用，对串行 stage 等于实际
  耗时；对可并发的批处理（`insight_analyze_batch`）则是**整批跨度**，不是单页耗时。
- **P50 / P95**：把同一 stage 在多次 run（或多年/多章）里的 span 样本汇总求分位。
  只有一条 run 时 P50 = P95 = 该 run 的实测值。
- 本脚本**只度量，不改任何代码或数据**。

用法
----
    python scripts/stage_profile.py                         # 默认 data-v2，全部 book
    python scripts/stage_profile.py --book 22da1ed8-f3f5-414c-8574-1785e7a51a16
    python scripts/stage_profile.py --data-dir /path/to/data-v2 --json out.json
    python scripts/stage_profile.py --runs-only completed    # 只看已完成 run
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# 允许直接 `python scripts/stage_profile.py` 运行（把仓库根加入 sys.path）。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import select  # noqa: E402

from src.backend_v2.storage.database import (  # noqa: E402
    create_sqlite_engine,
    database_path_for,
)
from src.backend_v2.storage.schema import (  # noqa: E402
    analysis_runs,
    job_events,
    jobs,
)

DEFAULT_DATA_ROOT = _REPO_ROOT / "data-v2"

# 展示顺序：先 page 批处理，再派生 stage（与分析流水线实际执行顺序一致）。
_STAGE_ORDER = (
    "insight_analyze_batch",
    "insight_build_layer_0",
    "insight_build_layer_1",
    "insight_build_layer_2",
    "insight_stage_overview_story_summary",
    "insight_stage_overview_no_spoiler",
    "insight_stage_compressed_context",
    "insight_stage_timeline",
    "insight_stage_entity_alignment",
    "insight_publish_run",
    "insight_stage_vectors",
    "insight_build_vectors",
)


def _to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    # SQLite 里可能是 'YYYY-MM-DD HH:MM:SS.ffffff' 文本。
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt) + 4] if "%f" not in fmt else text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _percentile(sorted_values: list[float], pct: float) -> float:
    """线性插值分位数（与 numpy 默认 'linear' 一致）。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    weight = rank - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def collect_stage_spans(engine, *, book_id: str | None, runs_status: str | None) -> dict[str, list[float]]:
    """返回 {stepKind: [span_seconds, ...]}（每条 job 每个 stage 一个样本）。"""
    spans: dict[str, list[float]] = defaultdict(list)

    with engine.connect() as conn:
        run_query = select(analysis_runs.c.id, analysis_runs.c.book_id, analysis_runs.c.status)
        if book_id:
            run_query = run_query.where(analysis_runs.c.book_id == book_id)
        if runs_status:
            run_query = run_query.where(analysis_runs.c.status == runs_status)
        run_ids = [row[0] for row in conn.execute(run_query)]

        # 一次 run 的 stage job 通过 jobs.analysis_run_id 关联；derived_rebuild 用自身 job id。
        job_query = select(jobs.c.id, jobs.c.analysis_run_id, jobs.c.kind, jobs.c.finished_at)
        if book_id:
            job_query = job_query.where(jobs.c.book_id == book_id)
        job_rows = list(conn.execute(job_query))
        job_ids = {row[0] for row in job_rows}
        finished_at_by_job: dict[str, datetime | None] = {
            row[0]: _to_dt(row[3]) for row in job_rows
        }
        # 也包含通过 analysis_run 关联的 job（scope 可能不同）。
        if run_ids:
            for row in conn.execute(
                select(jobs.c.id, jobs.c.finished_at).where(jobs.c.analysis_run_id.in_(run_ids))
            ):
                job_ids.add(row[0])
                finished_at_by_job.setdefault(row[0], _to_dt(row[1]))

        if not job_ids:
            return {}

        events = conn.execute(
            select(
                job_events.c.job_id,
                job_events.c.created_at,
                job_events.c.payload_json,
            )
            .where(job_events.c.event_type == "step_started")
            .where(job_events.c.job_id.in_(job_ids))
            .order_by(job_events.c.job_id, job_events.c.created_at)
        )

        # 每个 job 内按时间顺序记录 (dt, stepKind)。
        per_job: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
        for job_id, created_at, payload_json in events:
            dt = _to_dt(created_at)
            if dt is None:
                continue
            try:
                payload = json.loads(payload_json or "{}")
            except (ValueError, TypeError):
                payload = {}
            step_kind = payload.get("stepKind")
            if not isinstance(step_kind, str) or not step_kind:
                continue
            per_job[job_id].append((dt, step_kind))

    # 每个 job：把相邻同类 step 归为一段，段耗时 = 下一事件时间 − 本段首事件时间；
    # 最后一段用 job.finished_at 收尾。累加到对应 stepKind 的样本。
    for job_id, ordered in per_job.items():
        ordered.sort(key=lambda item: item[0])
        job_finish = finished_at_by_job.get(job_id)
        index = 0
        while index < len(ordered):
            start_dt, kind = ordered[index]
            last_dt = start_dt
            index += 1
            while index < len(ordered) and ordered[index][1] == kind:
                last_dt = ordered[index][0]
                index += 1
            if index < len(ordered):
                end_dt = ordered[index][0]
            else:
                end_dt = job_finish if job_finish is not None and job_finish >= last_dt else last_dt
            spans[kind].append(max(0.0, (end_dt - start_dt).total_seconds()))
    return spans


def summarize(spans: dict[str, list[float]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    order = {k: i for i, k in enumerate(_STAGE_ORDER)}
    for kind in sorted(spans, key=lambda k: (order.get(k, 999), k)):
        samples = sorted(spans[kind])
        rows.append(
            {
                "stage": kind,
                "samples": len(samples),
                "p50_seconds": round(_percentile(samples, 0.50), 2),
                "p95_seconds": round(_percentile(samples, 0.95), 2),
                "max_seconds": round(samples[-1], 2),
                "mean_seconds": round(statistics.fmean(samples), 2) if samples else 0.0,
            }
        )
    return rows


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m{rem:04.1f}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h{minutes:02d}m{rem:04.1f}s"


def _print_table(rows: list[dict[str, Any]], *, book_id: str | None) -> None:
    scope = f"book={book_id}" if book_id else "全部书籍"
    print(f"\n分析流水线 stage 耗时剖析（{scope}）")
    print("口径：stage 跨度 = 同一 job 内该 stage 首末 step_started 的时间差；"
          "批处理 stage 为整批跨度。\n")
    header = f"{'stage':40} {'样本':>4} {'P50':>10} {'P95':>10} {'max':>10} {'mean':>10}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['stage']:40} {row['samples']:>4} "
            f"{_fmt_duration(row['p50_seconds']):>10} "
            f"{_fmt_duration(row['p95_seconds']):>10} "
            f"{_fmt_duration(row['max_seconds']):>10} "
            f"{_fmt_duration(row['mean_seconds']):>10}"
        )
    if not rows:
        print("（没有可用的 step_started 事件——该书籍可能尚未分析或数据目录不正确）")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MiyakoKB 分析流水线 stage 性能剖析（只读）")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_ROOT), help="data-v2 根目录（默认 data-v2）")
    parser.add_argument("--book", default=None, help="只统计某本书（book_id UUID）")
    parser.add_argument(
        "--runs-only",
        dest="runs_status",
        default=None,
        help="只统计指定状态的 analysis_run（如 completed / completed_with_errors）",
    )
    parser.add_argument("--json", default=None, help="把结果同时写入该 JSON 文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认非 UTF-8，中文表头会乱码；显式切到 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    args = parse_args(argv)
    data_root = Path(args.data_dir).expanduser().resolve()
    db_path = database_path_for(data_root)
    if not db_path.is_file():
        print(f"找不到数据库：{db_path}", file=sys.stderr)
        return 1

    engine = create_sqlite_engine(db_path)
    try:
        spans = collect_stage_spans(
            engine,
            book_id=args.book,
            runs_status=args.runs_status,
        )
    finally:
        engine.dispose()

    rows = summarize(spans)
    _print_table(rows, book_id=args.book)

    if args.json:
        out = Path(args.json).expanduser().resolve()
        out.write_text(
            json.dumps({"book": args.book, "data_dir": str(data_root), "stages": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n结果已写入：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""验收点6：跨章节实体消歧观察分析器（只读）。

用法: python verify_acceptance6_alignment.py <book_id>

分析 entity_alignment 产物:
  1. 各类目实体计数 / slug_mode / 别名总量 / auto_merge_log 总量
  2. 高频角色 Top10：频次、别名数、页码跨度、章节覆盖
  3. auto_merge_log 健全性: raw/canonical 非空、规则分布、置信度分布
  4. 金额/日期类概念独立性: 同类不同值不得误并
  5. 未知实体回退统计 + 实体ID格式校验
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import create_engine, select

from src.backend_v2.storage.schema import (
    analysis_page_results,
    analysis_runs,
    chapters,
    pages,
)
from src.backend_v2.insight.derived import InsightDerivedRepository

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "data-v2" / "saber.sqlite3"

CATEGORY_PREFIX = {
    "characters": "character_",
    "items": "item_",
    "locations": "location_",
    "abilities": "ability_",
    "concepts": "concept_",
}

MONEY_DATE_PATTERN = re.compile(
    r"(日元|円|元|融资|亿|万|日$|日)|(\d{2,4}年|\d{1,2}月|\d{1,2}日)"
)


def frequency(entity: dict) -> int:
    # 生产形态: align_entities 直接维护 total_frequency（含 canonical 名自身计数）。
    # alias_details 只统计别名命中，不含 canonical 名 → 仅作旧产物兜底。
    tf = entity.get("total_frequency")
    if isinstance(tf, int) and tf > 0:
        return tf
    return sum(entity.get("alias_details", {}).values())


def main(book_id: str) -> int:
    engine = create_engine(f"sqlite:///{DB_PATH}")
    repo = InsightDerivedRepository(engine)
    art = repo.get_artifact(book_id=book_id, kind="entity_alignment", template="default")
    if art is None:
        print(f"FAIL: 无 entity_alignment 产物 book={book_id}")
        return 1
    payload = art["payload"]
    entities = payload["canonical_entities"]
    alias_map = payload["alias_map"]
    merge_log = payload["auto_merge_log"]

    print("=" * 72)
    print(f"book={book_id}  run={art.get('runId')}  status={art['status']}  rev={art['revision']}")
    print(f"slug_mode={payload['slug_mode']}  version={payload['version']}")
    print("=" * 72)

    # 产物可能由手动 rebuild 产生（run_id 为空）→ 回退到本书最近一次分析 run 做 pages→章 映射，
    # 否则章覆盖全为 0（仅影响本分析器的展示，不代表产物内容缺失）。
    map_run_id = art.get("runId")
    if map_run_id is None:
        with engine.connect() as conn:
            map_run_id = conn.execute(
                select(analysis_runs.c.id)
                .where(analysis_runs.c.book_id == book_id)
                .order_by(analysis_runs.c.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if map_run_id:
            print(f"[info] 产物无 run_id，pages→章 映射回退到最近 run={map_run_id}")

    # -- 1. 类目计数 -----------------------------------------------------
    cat_counter = Counter(e["category"] for e in entities)
    print(f"\n[1] 实体总量 {len(entities)}，别名映射 {len(alias_map)}，"
          f"自动归并日志 {len(merge_log)}")
    for cat in ("characters", "items", "locations", "abilities", "concepts"):
        print(f"    {cat:12s}: {cat_counter.get(cat, 0)}")

    # -- 页码→章节映射（生产 scope=full 形态: 实体 pages = 全书全局页码
    #    page_number_snapshot；限定产物 run 的结果避免跨 run 串页）--
    with engine.connect() as conn:
        rows = conn.execute(
            select(analysis_page_results.c.page_number_snapshot,
                   chapters.c.ordinal, chapters.c.title)
            .join(pages, pages.c.id == analysis_page_results.c.page_id_snapshot)
            .join(chapters, chapters.c.id == pages.c.chapter_id)
            .where(chapters.c.book_id == book_id,
                   analysis_page_results.c.run_id == map_run_id)
        ).fetchall()
    page_to_chapter = {p: c for p, c, _ in rows}
    chapter_titles = {c: t for _, c, t in rows}
    dup_global = [p for p, cnt in Counter(r[0] for r in rows).items() if cnt > 1]
    if dup_global:
        print(f"    [warn] {len(dup_global)} 个全局页码重复，映射取后者")
    total_chapters = len(set(page_to_chapter.values()))
    print(f"    章节总数(已析)={total_chapters}  本run页映射={len(page_to_chapter)}")

    # valid_chapters 生产形态诊断：scope=full 单次调用 chapter_id=""，
    # 生产产物应全部为 [""]（章归属丢失，观察发现#2）；逐章调用才有真实章ID。
    vc_empty = sum(1 for e in entities
                   if set(e.get("valid_chapters", [])) <= {""})
    print(f"    valid_chapters 为空串/缺失的实体: {vc_empty}/{len(entities)}"
          f"（生产 full-run 预期=全部）")

    def chapters_of(entity: dict) -> set[int]:
        return {page_to_chapter[p] for p in entity.get("pages", []) if p in page_to_chapter}

    # -- 2. 高频角色 Top10 -----------------------------------------------
    print("\n[2] 角色频次 Top10（频次=total_frequency，语义为去重后的出现页数）")
    chars = [e for e in entities if e["category"] == "characters"]
    chars.sort(key=frequency, reverse=True)
    for e in chars[:10]:
        chs = chapters_of(e)
        span = f"{min(chs)}–{max(chs)}" if chs else "-"
        print(f"    {e['canonical_name']:<14s} id={e['entity_id']:<44s} "
              f"频次={frequency(e):>4d} 别名={len(e.get('aliases', [])):>2d} "
              f"章覆盖={len(chs):>3d}/{total_chapters} (序 {span})")

    # -- 3. auto_merge_log 健全性 ---------------------------------------
    print("\n[3] auto_merge_log 健全性")
    bad = [m for m in merge_log if not m.get("raw_name") or not m.get("canonical_name")]
    print(f"    空 raw/canonical 记录: {len(bad)} (旧版 None→None 缺陷应=0)")
    rule_dist = Counter(m.get("rule", "?") for m in merge_log)
    for rule, n in rule_dist.most_common():
        print(f"    规则 {rule}: {n}")
    conf_dist = Counter(m.get("confidence", "?") for m in merge_log)
    print(f"    置信度分布: {dict(conf_dist)}")
    if merge_log:
        print(f"    示例: {json.dumps(merge_log[0], ensure_ascii=False)[:180]}")

    # -- 4. 金额/日期概念独立性 ------------------------------------------
    print("\n[4] 金额/日期类概念独立性（同类不同值应各自独立 ID）")
    md = [e for e in entities if e["category"] == "concepts"
          and MONEY_DATE_PATTERN.search(e["canonical_name"])]
    md.sort(key=frequency, reverse=True)
    ids = [e["entity_id"] for e in md]
    dup = len(ids) - len(set(ids))
    print(f"    命中概念 {len(md)} 个，重复 ID {dup} 个（应=0）")
    for e in md[:12]:
        print(f"    {e['canonical_name']:<24s} id={e['entity_id']:<44s} 频次={frequency(e)}")

    # -- 5. ID 格式 + 回退统计 --------------------------------------------
    print("\n[5] 实体ID格式与回退统计")
    malformed = []
    for e in entities:
        prefix = CATEGORY_PREFIX.get(e["category"])
        if prefix and not e["entity_id"].startswith(prefix):
            malformed.append((e["category"], e["entity_id"]))
        if not re.fullmatch(r"[a-z0-9_]+", e["entity_id"]):
            malformed.append((e["category"], e["entity_id"]))
    print(f"    格式异常 ID: {len(malformed)} (应=0)")
    for cat, eid in malformed[:5]:
        print(f"      {cat}: {eid}")
    pinyin_fallback = [e for e in entities if e["entity_id"].startswith("pinyin_")]
    print(f"    pinyin_ 回退实体: {len(pinyin_fallback)}")
    for e in pinyin_fallback[:5]:
        print(f"      {e['canonical_name']} -> {e['entity_id']}")

    # -- 6. 跨章稳定观察：非角色类目 Top5 --------------------------------
    for cat in ("items", "locations", "concepts"):
        ents = [e for e in entities if e["category"] == cat]
        ents.sort(key=frequency, reverse=True)
        print(f"\n[6.{cat}] {cat} Top5")
        for e in ents[:5]:
            chs = chapters_of(e)
            print(f"    {e['canonical_name']:<20s} 频次={frequency(e):>3d} "
                  f"章覆盖={len(chs):>3d}/{total_chapters} id={e['entity_id'][:40]}")

    # -- 7. 词频语义：按页去重 -------------------------------------------
    # 语义约定（2026-10-19 用户拍板）：total_frequency = 去重后的出现页数。
    # 同一页因多个 run 的已发布结果同时被喂入时不得重复计数。
    print("\n[7] 词频按页去重一致性（频次应等于去重后页数）")
    mismatched = []
    for e in entities:
        tf = e.get("total_frequency")
        if not isinstance(tf, int):
            continue
        unique_pages = len(set(e.get("pages", [])))
        if tf != unique_pages:
            mismatched.append((e["canonical_name"], tf, unique_pages))
    print(f"    频次 != 去重页数 的实体: {len(mismatched)}/{len(entities)}（应=0）")
    for name, tf, pages_n in mismatched[:8]:
        print(f"      {name}: total_frequency={tf} 去重页数={pages_n}")
    top = sorted(entities, key=frequency, reverse=True)[:5]
    for e in top:
        print(f"    {e['canonical_name']:<16s} 频次={frequency(e):>4d} 页数={len(set(e.get('pages', []))):>4d}")

    print("\nANALYSIS DONE")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))

# -*- coding: utf-8 -*-
"""验收点6下游验证：图谱/时间线/概览/问答状态（对指定书，只读 API + 少量 DB）。

用法: python verify_acceptance6_downstream.py <book_id> [--api http://127.0.0.1:5000]
"""
import argparse
import json
import sys
import urllib.request
from collections import Counter

API = "http://127.0.0.1:5000"


def get(path: str, timeout: int = 120):
    with urllib.request.urlopen(f"{API}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    global API
    ap = argparse.ArgumentParser()
    ap.add_argument("book_id")
    ap.add_argument("--api", default=API)
    args = ap.parse_args()
    API = args.api
    book_id = args.book_id

    print("=" * 72)
    print(f"下游验证 book={book_id}")
    print("=" * 72)

    # [1] 图谱
    print("\n[1] 共现图谱 GET /api/v2/insight/graph")
    try:
        g = get(f"/api/v2/insight/graph?bookId={book_id}")
        nodes, edges = g.get("nodes", []), g.get("edges", [])
        cat = Counter(n.get("category", "?") for n in nodes)
        deg = Counter()
        for e in edges:
            deg[e.get("source")] += 1
            deg[e.get("target")] += 1
        isolated = sum(1 for n in nodes if deg.get(n["id"], 0) == 0)
        top = sorted(nodes, key=lambda n: -n.get("frequency", 0))[:5]
        print(f"    节点={len(nodes)} 边={len(edges)} 孤立节点={isolated}")
        print(f"    类目分布: {dict(cat)}")
        for n in top:
            print(f"    Top {n.get('category','?'):<10} {n.get('name')} 频次={n.get('frequency')} 度={deg.get(n['id'],0)}")
        if edges:
            ws = Counter(e.get("weight") for e in edges)
            print(f"    边权样本: {dict(list(ws.most_common(5)))}")
    except Exception as exc:  # noqa: BLE001
        print(f"    [FAIL] {exc}")

    # [2] 时间线
    print("\n[2] 时间线 GET /api/v2/insight/timeline")
    try:
        t = get(f"/api/v2/insight/timeline?bookId={book_id}")
        events = t.get("events", [])
        pages_hit = sorted({p for ev in events for p in ev.get("page_numbers", [])})
        chars = t.get("characters", [])
        span = (pages_hit[0], pages_hit[-1]) if pages_hit else ("-","-")
        print(f"    事件={len(events)} mode={t.get('mode')} status={t.get('status')}")
        print(f"    人物条目={len(chars)} 页覆盖={len(pages_hit)}/{t.get('pageCount')}(范围 {span[0]}–{span[1]})")
        for ev in events[:3]:
            print(f"    示例 p{ev.get('page_numbers')}: {ev.get('summary','')[:80]}…")
    except Exception as exc:  # noqa: BLE001
        print(f"    [FAIL] {exc}")

    # [3] 概览产物（已存产物读取，不触发上游）
    for tpl in ("no_spoiler", "story_summary"):
        print(f"\n[3] 概览 GET /api/v2/insight/artifacts/overviews/{tpl}")
        try:
            o = get(f"/api/v2/insight/artifacts/overviews/{tpl}?bookId={book_id}")
            payload = o.get("payload") or {}
            text = payload.get("content") or payload.get("text") or json.dumps(payload, ensure_ascii=False)
            print(f"    status={o.get('status')} rev={o.get('revision')} run={str(o.get('runId'))[:8]} len={len(text)}")
            print(f"    开头: {text[:120]}…")
        except Exception as exc:  # noqa: BLE001
            print(f"    [FAIL] {exc}")

    # [4] 问答状态
    print("\n[4] 问答状态 GET /api/v2/insight/qa/status")
    try:
        s = get(f"/api/v2/insight/qa/status?bookId={book_id}")
        keys = {k: v for k, v in s.items() if not isinstance(v, (list, dict))}
        print(f"    {keys}")
        for k, v in s.items():
            if isinstance(v, dict):
                print(f"    {k}: {json.dumps(v, ensure_ascii=False)[:200]}")
    except Exception as exc:  # noqa: BLE001
        print(f"    [FAIL] {exc}")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

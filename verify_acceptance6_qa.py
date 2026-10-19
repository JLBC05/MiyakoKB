# -*- coding: utf-8 -*-
"""验收点6 问答验证：POST /api/v2/insight/books/<id>/qa（SSE 流式）。

用法: python verify_acceptance6_qa.py <book_id> "问题" [--mode vector] [--topk 8]
"""
import argparse
import json
import sys
import time
import urllib.request

API = "http://127.0.0.1:5000"


def main() -> int:
    global API
    ap = argparse.ArgumentParser()
    ap.add_argument("book_id")
    ap.add_argument("question")
    ap.add_argument("--api", default=API)
    ap.add_argument("--mode", default="exact", help="exact|global")
    ap.add_argument("--topk", type=int, default=8)
    ap.add_argument("--threshold", type=float, default=None)
    args = ap.parse_args()
    API = args.api

    body = {"question": args.question, "mode": args.mode, "topK": args.topk}
    if args.threshold is not None:
        body["threshold"] = args.threshold
    req = urllib.request.Request(
        f"{API}/api/v2/insight/books/{args.book_id}/qa",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    answer_parts: list[str] = []
    citations: list = []
    resolved: dict = {}
    error = None
    with urllib.request.urlopen(req, timeout=600) as resp:
        event = None
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                if event == "status":
                    print(f"[status] {data.get('status')} ({time.time()-t0:.1f}s)")
                elif event == "context":
                    citations = data.get("citations", [])
                    resolved = data.get("resolvedEntities", {})
                    print(f"[context] mode={data.get('mode')} 引用={len(citations)} "
                          f"匹配实体={len(resolved.get('matched_entities', []))} "
                          f"结构化查询={resolved.get('is_structured_query')}")
                    for e in resolved.get("matched_entities", [])[:5]:
                        if isinstance(e, dict):
                            print(f"    实体: {e.get('name', e.get('canonical_name', e))} "
                                  f"({e.get('category', '?')})")
                        else:
                            print(f"    实体: {e}")
                    for c in citations[:3]:
                        print(f"    引用: {json.dumps(c, ensure_ascii=False)[:160]}")
                elif event == "chunk":
                    answer_parts.append(data.get("text", ""))
                elif event == "error":
                    error = data
                elif event == "done":
                    pass
    print(f"\n[answer] ({time.time()-t0:.1f}s, {len(answer_parts)} chunks)")
    print("".join(answer_parts))
    if error:
        print(f"\n[ERROR] {json.dumps(error, ensure_ascii=False)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

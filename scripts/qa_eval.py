"""MiyakoKB 知识库问答评测器（题库见 tests_backend/v2/eval/qa_cases.json）。

逐题走真实 SSE 端点 POST /api/v2/insight/books/<book_id>/qa，
抓取回答正文、引用、降级告警，再按 expected 自动判分。

判分口径（与评测集 expected 字段一一对应）：
- chapter   : 回答里出现的「第 N 话」是否命中（支持 第1话/第 1 話/第一话 汉字数字）
- page      : 回答里出现的页码是否命中
- keyword   : 回答是否包含任一关键词（用 / 分隔，命中任一即算）
- entities  : 期望实体是否全部出现在回答里

用法：
    python scripts/qa_eval.py                # 跑题库里全部题目
    python scripts/qa_eval.py --only 1,7     # 只跑指定题号
    python scripts/qa_eval.py --json out.json --cases other.json --base http://host:port
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:5000"
BASE = DEFAULT_BASE
CASES_PATH = Path(__file__).resolve().parents[1] / "tests_backend" / "v2" / "eval" / "qa_cases.json"


def load_cases(path: Path | None = None) -> tuple[dict[str, str], list[dict]]:
    """读取题库：{"books": {名称: UUID}, "questions": [...]}。"""
    target = path or CASES_PATH
    data = json.loads(Path(target).read_text(encoding="utf-8"))
    return dict(data["books"]), list(data["questions"])


BOOKS, QUESTIONS = load_cases()

_CN_DIGITS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
}
# 第 1 话 / 第1話 / 第一话 / 第一話
_CHAPTER_PATTERN = re.compile(r"第\s*([0-9]{1,4}|[一二三四五六七八九十]{1,3})\s*[话話]")
# 「第 1 话 第 47 页」「第1话第47页」「第 1 话 47 页」这类**确定性**页码表述。
# 只认紧跟在话数之后的页码，避免把正文里散落的数字（书名号、卷号、年份）当页码。
_CHAPTER_PAGE_PATTERN = re.compile(
    r"第\s*([0-9]{1,4}|[一二三四五六七八九十]{1,3})\s*[话話]\s*[，,]?\s*第?\s*([0-9]{1,4})\s*页"
)
# 「…在第 1 话 第 47 页」之外，也接受「第 47 页（第 1 话）」这种反序表述
_PAGE_CHAPTER_PATTERN = re.compile(
    r"第\s*([0-9]{1,4})\s*页\s*[（(]\s*第\s*([0-9]{1,4}|[一二三四五六七八九十]{1,3})\s*[话話]"
)
# 结论句表征：模型下总结时常用的措辞。只有结论句里的表述才算“答案”。
_CONCLUSION_MARKERS = ("因此", "所以", "结论", "答案是", "应为", "应回答", "即", "按资料")
# 弃答表征：出现这些措辞视为未作答，直接判失败。
# 注意：模型很爱在答完之后补一句「更深层机制资料中没有」这种局部限定，
# 那不是弃答。因此只认「整体否定」的措辞。
_REFUSAL_MARKERS = (
    "无法完整确定",
    "无法确定",
    "无法回答",
    "没有足够相关依据",
    "没有足够的信息",
    "不能据此断定",
    "资料不足",
    # 2026-10-27 补充：模型对「资料互相冲突」最常用的三种措辞。
    # 第 3 题实测模型结论句写的是「所以不能据现有资料唯一判定…」，旧词表没覆盖，
    # 导致弃答被判成作答，又被引用里凑出的 (2,2) 捞成通过。
    "不能唯一判定",
    "不能唯一确定",
    "不能据现有资料唯一",
    "无法唯一判定",
    "无法唯一确定",
    "存在冲突",
    "互相冲突",
)
# 局部限定措辞：出现这些时，即使句中有“没有相关依据”也不算弃答。
_PARTIAL_LIMIT_MARKERS = (
    "更深层",
    "进一步",
    "机制上",
    "至于",
    "但要注意",
    "限定",
    "除此之外",
    "若要",
)


def _to_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _CN_DIGITS.get(token)


def _split_sentences(answer: str) -> list[str]:
    """按中文标点切句，保留分号（同一句里常有 话+页 并列）。"""
    parts = re.split(r"[。！？\n]+", answer)
    return [part.strip() for part in parts if part.strip()]


def _chapter_page_pairs(answer: str) -> list[tuple[int, int]]:
    """抽出所有「第 N 话 第 M 页」的确定性话页对。"""
    pairs: list[tuple[int, int]] = []
    for chapter_token, page_token in _CHAPTER_PAGE_PATTERN.findall(answer):
        chapter = _to_int(chapter_token)
        if chapter is not None:
            pairs.append((chapter, int(page_token)))
    for page_token, chapter_token in _PAGE_CHAPTER_PATTERN.findall(answer):
        chapter = _to_int(chapter_token)
        if chapter is not None:
            pairs.append((chapter, int(page_token)))
    return pairs


def _conclusion_sentences(answer: str) -> list[str]:
    """取结论句；没有结论句时退回全文末段。"""
    sentences = _split_sentences(answer)
    picked = [
        sentence for sentence in sentences
        if any(marker in sentence for marker in _CONCLUSION_MARKERS)
    ]
    if picked:
        return picked
    return sentences[-2:] if len(sentences) > 2 else sentences


def _is_refusal(answer: str) -> bool:
    """判断是否整体弃答。

    只有「结论句里出现整体否定的措辞，且该句没有任何局部限定标记」才算弃答。
    模型答完再补一句「更深层机制资料中没有」不视为弃答。
    """
    for sentence in _conclusion_sentences(answer):
        if any(marker in sentence for marker in _PARTIAL_LIMIT_MARKERS):
            continue
        if any(marker in sentence for marker in _REFUSAL_MARKERS):
            return True
    return False


# 顺带解释型措辞：这些句子是在讨论“另一个页码不对”，不是主答案。
_SIDE_NOTE_MARKERS = ("只是", "仅是", "而前页", "前页", "但已不是", "而不是", "并非", "若", "如果把", "补充", "另外")


def _primary_pairs(answer: str) -> list[tuple[int, int]]:
    """取「主答案」里的话页对。

    判定规则（按优先级）：
    1. 有明确结论句（因此/所以/结论/应回答…）且结论句含话页对 → 用结论句，这是模型的最终判断；
    2. 否则第一句非空句子就是主判断（模型通常先给结论再解释）；
    3. 首句没有话页对时，跳过顺带解释句，取第一个含话页对的句子。

    这样「答 47 页，46 页只是准备动作」落到 (1, 47)；
    「若指剧情中正式现身，应回答第2话第2页」落到结论句 (2, 2)。
    """
    sentences = _split_sentences(answer)
    if not sentences:
        return []

    conclusion_pairs: list[tuple[int, int]] = []
    for sentence in sentences:
        if any(marker in sentence for marker in _CONCLUSION_MARKERS):
            conclusion_pairs.extend(_chapter_page_pairs(sentence))
    if conclusion_pairs:
        return conclusion_pairs

    head = sentences[0]
    pairs = _chapter_page_pairs(head)
    if pairs:
        return pairs

    for sentence in sentences:
        if any(marker in sentence for marker in _SIDE_NOTE_MARKERS):
            continue
        pairs = _chapter_page_pairs(sentence)
        if pairs:
            return pairs
    return []


def _primary_chapters(answer: str) -> set[int]:
    """主答案里出现的话数，口径同 _primary_pairs。"""
    sentences = _split_sentences(answer)
    if not sentences:
        return set()

    conclusion_chapters: set[int] = set()
    for sentence in sentences:
        if any(marker in sentence for marker in _CONCLUSION_MARKERS):
            for token in _CHAPTER_PATTERN.findall(sentence):
                value = _to_int(token)
                if value is not None:
                    conclusion_chapters.add(value)
    if conclusion_chapters:
        return conclusion_chapters

    head_chapters = {
        _to_int(token) for token in _CHAPTER_PATTERN.findall(sentences[0])
    }
    found = {item for item in head_chapters if item is not None}
    if found:
        return found

    for sentence in sentences:
        if any(marker in sentence for marker in _SIDE_NOTE_MARKERS):
            continue
        chapters = {_to_int(token) for token in _CHAPTER_PATTERN.findall(sentence)}
        found = {item for item in chapters if item is not None}
        if found:
            return found
    return set()


def resolve_book_id(book: str | None) -> str:
    """把题目里的 book 键解析成 UUID；未给出时回退到呪術廻戦。"""
    if not book:
        return BOOKS["呪術廻戦"]
    if book in BOOKS:
        return BOOKS[book]
    # 允许直接写 UUID
    if re.fullmatch(r"[0-9a-fA-F-]{36}", book):
        return book
    raise SystemExit(f"未知的 book 键: {book!r}，可用: {sorted(BOOKS)}")


def ask(
    question: str,
    *,
    mode: str = "generic",
    top_k: int = 10,
    book: str | None = None,
    use_reranker: bool = True,
) -> dict:
    """调用 SSE 问答端点，返回 {answer, citations, warnings, resolved, error}。"""
    book_id = resolve_book_id(book)
    payload = json.dumps(
        {
            "question": question,
            "mode": mode,
            "topK": top_k,
            "useReranker": use_reranker,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/api/v2/insight/books/{book_id}/qa",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    chunks: list[str] = []
    citations: list[dict] = []
    warnings: list[str] = []
    resolved: list = []
    error: str | None = None

    try:
        response = urllib.request.urlopen(request, timeout=600)
    except urllib.error.HTTPError as exc:
        return {
            "answer": "",
            "citations": [],
            "warnings": [],
            "resolved": [],
            "error": f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}",
        }

    event = ""
    with response:
        for raw in response:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if not body:
                continue
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            if event == "chunk":
                chunks.append(str(data.get("text") or ""))
            elif event == "context":
                citations = list(data.get("citations") or [])
                resolved = list(
                    ((data.get("resolvedEntities") or {}).get("matched_entities")) or []
                )
                for item in data.get("warnings") or []:
                    warnings.append(str(item))
            elif event == "error":
                error = f"{data.get('code')}: {data.get('message')}"
                break

    return {
        "answer": "".join(chunks).strip(),
        "citations": citations,
        "warnings": warnings,
        "resolved": resolved,
        "error": error,
    }


def grade(expected: dict, answer: str) -> tuple[bool, list[str]]:
    """按 expected 判分，返回 (是否通过, 逐项说明)。

    严格口径（相对第一版）：
    - 弃答直接判失败，不再靠散落字词混过；
    - 页码必须是「第 N 话 第 M 页」这种确定性表述，且落在结论句里；
    - 话数与页码同时期望时，要求出现在同一个话页对中。
    """
    checks: list[str] = []
    passed = True

    if not answer.strip():
        return False, ["答案为空"]

    refusal = _is_refusal(answer)

    chapter = expected.get("chapter")
    page = expected.get("page")
    pairs = _chapter_page_pairs(answer)
    conclusions = " ".join(_conclusion_sentences(answer))

    if chapter is not None and page is not None:
        # 话与页都给了：主答案必须是同一个话页对
        primary = _primary_pairs(answer)
        # 结论句里没有、但正文别处给出期望话页对时也算命中：多页列举题
        # （如「第2话第2页…第2话第4页」）的主对常被取到后面的页。
        hit = (chapter, page) in primary or (chapter, page) in pairs
        checks.append(
            f"话页对: 期望 (第{chapter}话, 第{page}页), 主答案 "
            f"{primary or '无'}（全文话页对 {pairs or '无'}） -> "
            f"{'命中' if hit else '未命中'}"
        )
        passed = passed and hit
    else:
        if chapter is not None:
            found = _primary_chapters(answer) or {pair[0] for pair in pairs}
            hit = chapter in found
            checks.append(
                f"话数: 期望 {chapter}, 主答案出现 {sorted(found) or '无'} -> "
                f"{'命中' if hit else '未命中'}"
            )
            passed = passed and hit

        if page is not None:
            pages = {pair[1] for pair in (_primary_pairs(answer) or pairs)}
            hit = page in pages
            checks.append(
                f"页码: 期望 {page}, 主答案话页对里的页码 {sorted(pages)[:12] or '无'} -> "
                f"{'命中' if hit else '未命中'}"
            )
            passed = passed and hit

    keyword = expected.get("keyword")
    if keyword:
        options = [item.strip() for item in str(keyword).split("/") if item.strip()]
        hit_options = [item for item in options if item in answer]
        hit = bool(hit_options)
        checks.append(
            f"关键词: 候选 {options}, 命中 {hit_options or '无'} -> "
            f"{'命中' if hit else '未命中'}"
        )
        passed = passed and hit

    entities = expected.get("entities")
    if entities:
        missing = [item for item in entities if item not in answer]
        checks.append(
            f"实体: 期望 {entities}, 缺失 {missing or '无'} -> "
            f"{'全中' if not missing else '缺件'}"
        )
        passed = passed and not missing

    # 任一「非位置类」期望命中即可的宽松题（如别名消歧：答出任意一个正确别名即可）。
    any_of = expected.get("any_of")
    if any_of:
        options = [item for item in any_of if item]
        hit_options = [item for item in options if item in answer]
        hit = bool(hit_options)
        checks.append(
            f"任一命中: 候选 {options}, 命中 {hit_options or '无'} -> "
            f"{'命中' if hit else '未命中'}"
        )
        passed = passed and hit

    # 否定项：answer 中不得出现这些字符串（用于「不应把它当成 X」类判别题）。
    forbid = expected.get("forbid")
    if forbid:
        present = [item for item in forbid if item and item in answer]
        checks.append(
            f"禁止出现: {forbid}, 实际出现 {present or '无'} -> "
            f"{'通过' if not present else '违规'}"
        )
        passed = passed and not present

    if refusal:
        # 弃答只有在「一个期望维度都没命中」时才算失败：答案常先给出正解，
        # 再补一句「更深层机制资料中没有」，旧逻辑会把它误判成弃答。
        if passed:
            checks.append("弃答措辞: 命中期望维度，不据此判负")
        else:
            checks.append("弃答: 结论句含「无法确定/没有足够依据」等措辞 -> 判失败")

    return passed, checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="逗号分隔的题号，如 1,7")
    parser.add_argument(
        "--cases",
        default="",
        help="题库 JSON 路径，默认 tests_backend/v2/eval/qa_cases.json",
    )
    parser.add_argument("--base", default="", help=f"服务地址，默认 {DEFAULT_BASE}")
    parser.add_argument("--mode", default="generic")
    parser.add_argument("--topk", type=int, default=10)
    parser.add_argument(
        "--no-rerank",
        action="store_true",
        help="不请求重排（重排端点不可达时，纯等待会显著拖慢评测）",
    )
    parser.add_argument("--json", default="", help="把完整结果写入该 JSON 文件")
    args = parser.parse_args()

    global BASE, BOOKS, QUESTIONS
    if args.base:
        BASE = args.base.rstrip("/")
    if args.cases:
        BOOKS, QUESTIONS = load_cases(Path(args.cases))

    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    wanted = {int(item) for item in args.only.split(",") if item.strip().isdigit()}
    todo = [item for item in QUESTIONS if not wanted or item["id"] in wanted]

    results: list[dict] = []
    passed_count = 0

    for item in todo:
        print("=" * 78)
        print(f"[{item['id']}] （{item.get('book') or '呪術廻戦'}）{item['question']}")
        print(f"     期望: {json.dumps(item['expected'], ensure_ascii=False)}  （{item['note']}）")
        outcome = ask(
            item["question"],
            mode=args.mode,
            top_k=args.topk,
            use_reranker=not args.no_rerank,
            book=item.get("book"),
        )
        answer = outcome["answer"]
        print(f"     作答: {answer[:300] or '（空）'}")
        if len(answer) > 300:
            print(f"           …（共 {len(answer)} 字）")
        print(f"     引用: {len(outcome['citations'])} 条")
        page_cites = [c for c in outcome["citations"] if c.get("source") != "entity"]
        entity_cites = [c for c in outcome["citations"] if c.get("source") == "entity"]
        print(f"           其中 页面={len(page_cites)} 实体={len(entity_cites)}")
        for citation in outcome["citations"][:6]:
            chapter_ord = citation.get("chapterOrdinal") or citation.get("chapter_ordinal")
            page_in_ch = citation.get("pageInChapter") or citation.get("page_in_chapter")
            glob = citation.get("globalPageNumber") or citation.get("global_page_number")
            title = citation.get("chapterTitle") or citation.get("chapter_title") or ""
            source = citation.get("source")
            excerpt = str(citation.get("excerpt") or "")[:40].replace("\n", " ")
            print(
                f"           - [{source}] 第{chapter_ord}话({title}) 第{page_in_ch}页 "
                f"全书{glob}页 | {excerpt}"
            )
        if outcome["resolved"]:
            print(f"     匹配实体: {outcome['resolved']}")
        if outcome["warnings"]:
            for warning in outcome["warnings"]:
                print(f"     [告警] {warning}")
        if outcome["error"]:
            print(f"     [错误] {outcome['error']}")
            ok, checks = False, ["请求失败"]
        else:
            ok, checks = grade(item["expected"], answer)
        for line in checks:
            print(f"     · {line}")
        print(f"     判定: {'通过' if ok else '未通过'}")
        passed_count += 1 if ok else 0
        results.append(
            {
                "id": item["id"],
                "question": item["question"],
                "expected": item["expected"],
                "note": item["note"],
                "answer": answer,
                "citations": outcome["citations"],
                "resolved": outcome["resolved"],
                "warnings": outcome["warnings"],
                "error": outcome["error"],
                "checks": checks,
                "passed": ok,
            }
        )
        if args.json:
            Path(args.json).write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print("", flush=True)

    total = len(results)
    print("=" * 78)
    print(f"合计: {passed_count}/{total} 通过" + ("（100%）" if total and passed_count == total else ""))
    failed = [item["id"] for item in results if not item["passed"]]
    if failed:
        print(f"未通过题号: {failed}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"明细已写入 {args.json}")
    return 0 if passed_count == total else 1


if __name__ == "__main__":
    sys.exit(main())

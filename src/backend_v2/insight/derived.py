"""Versioned Insight overview, timeline, compressed-context, and vector jobs."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import math
import os
from numbers import Real
from pathlib import Path
import re
from typing import Any, Callable, Protocol
import uuid

from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.engine import Connection

from src.backend_v2.auth.ownership import effective_owner_id
from src.backend_v2.redaction import redact_sensitive_text
from src.backend_v2.serialization import canonical_json as _json
from src.backend_v2.insight.page_schema import (
    ALLOWED_IMPORTANCE,
    InvalidPageAnalysis,
    deserialize_page_analysis,
    get_run_extra_entities,
    validate_persisted_page_analysis,
)
from src.backend_v2.insight.errors import InsightInvariant, InsightPrecondition
from src.backend_v2.insight.repository import (
    InsightConflict,
    InsightNotFound,
    OVERVIEW_TEMPLATES,
    contains_nonempty_text,
)
from src.backend_v2.insight.provider_runtime import (
    frozen_chat_config,
    frozen_embedding_config,
)
from src.backend_v2.insight.entity_alignment import (
    AlignmentConfig,
    CandidateEdge,
    EntityNode,
    GraphBuildResult,
    apply_decisions,
    build_alignment_graph,
    plan_requests,
    resolve_alignment_config,
    run_alignment_sync,
    summarize_graph,
)
from src.backend_v2.timestamps import utcnow
from src.backend_v2.jobs.repository import (
    AttemptFence,
    AttemptFenced,
    JobConflict,
    JobItemSpec,
    JobQueueRepository,
    JobSpec,
)
from src.backend_v2.settings.resolver import SettingsResolver
from src.backend_v2.storage.platform_repositories import SettingsRepository
from src.backend_v2.storage.schema import (
    analysis_artifacts,
    analysis_heads,
    analysis_layer_result_pages,
    analysis_layer_results,
    analysis_page_results,
    analysis_run_targets,
    analysis_runs,
    assets,
    books,
    chapters,
    pages,
    page_assets,
    timeline_characters,
    timeline_events,
    timeline_versions,
    vector_generations,
)
from src.shared.memory_errors import is_memory_allocation_error
from src.shared.user_logging import json_details, log_result

LOGGER = logging.getLogger("saber.worker.insight_derived")


DERIVED_KINDS = frozenset(
    {"overview", "compressed_context", "timeline", "vector", "entity_alignment"}
)
FINAL_ANALYSIS_RUN_STATUSES = frozenset(
    {"completed", "completed_with_errors"}
)


# 1. 显式加载 entity_slug_map.json 配置（不再从 cwd / 仓库目录隐式探测）
ENTITY_SLUG_MAP_FILENAME = "entity_slug_map.json"
_SLUG_MAP_CATEGORIES = ("characters", "items", "abilities", "locations", "concepts")


def empty_entity_slug_map() -> dict[str, dict[str, str]]:
    return {category: {} for category in _SLUG_MAP_CATEGORIES}


def load_entity_slug_map(config_path: str | Path | None) -> dict[str, dict[str, str]]:
    """加载实体到罗马音/英文 slug 的映射配置。

    只接受调用方显式传入的路径：历史实现会依次探测 cwd、仓库根与模块目录下的
    ``entity_slug_map.json``，导致任何遗留在工作目录里的作品专属映射表都被当成
    全局配置生效。现在路径缺失或文件不可用时返回空映射并记录日志。
    """
    if not config_path:
        LOGGER.info("未配置 entity_slug_map 路径，本次不使用外部 slug 映射")
        return empty_entity_slug_map()

    path = Path(config_path)
    if not path.is_file():
        LOGGER.warning("entity_slug_map 不存在，本次不使用外部 slug 映射：%s", path)
        return empty_entity_slug_map()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 配置损坏不应中断流水线
        LOGGER.warning("entity_slug_map 解析失败，本次不使用外部 slug 映射：%s（%s）", path, exc)
        return empty_entity_slug_map()

    if not isinstance(data, Mapping):
        LOGGER.warning("entity_slug_map 结构不是对象，本次不使用外部 slug 映射：%s", path)
        return empty_entity_slug_map()

    return {
        category: dict(data.get(category, {}) or {})
        for category in _SLUG_MAP_CATEGORIES
    }

CATEGORY_PREFIX = {
    "characters": "character",
    "items": "item",
    "abilities": "ability",
    "locations": "location",
    "concepts": "concept",
}


def pinyin_slug(text: str) -> str:
    """把实体名称统一转成 ASCII slug。

    优先用 pypinyin 生成拼音；pypinyin 缺失或结果为空时退化为保留原文本中的
    ASCII 字符。历史实现按类别分别查表（地点走硬编码词根表、其余走单字拼音表），
    两张表都是作品专属数据，已统一为同一套规则。
    """
    slug = ""
    try:
        import pypinyin
        py_list = pypinyin.lazy_pinyin(text)
        if py_list:
            slug = "_".join(py_list)
    except Exception:  # noqa: BLE001 - pypinyin 不可用时回退到 ASCII 字符
        slug = ""
    slug = re.sub(r'[^a-zA-Z0-9_]+', '_', slug)
    slug = re.sub(r'_+', '_', slug).strip('_').lower()
    if slug:
        return slug
    fallback = re.sub(r'[^a-zA-Z0-9_]+', '_', text)
    fallback = re.sub(r'_+', '_', fallback).strip('_').lower()
    return fallback or "entity"


def to_romanized_slug(
    category: str,
    text: str,
    slug_map: Mapping[str, Mapping[str, str]] | None = None,
    slug_mode: str = "pinyin",
) -> str:
    """根据 category 和 text 生成 slug。

    规则：
    1. "romanized" 模式：
       - 优先命中 entity_slug_map 配置 → 返回正式罗马音/英文 slug；
       - 未命中配置 → 回退并在 slug 中添加 'romanized_' 前缀供 list_pinyin_entities 提取审核。
    2. "pinyin" 模式：
       - 全量采用拼音格式生成 slug（如 'hu_zhang_you_ren'），不附加前缀。
    """
    mode = (slug_mode or "pinyin").strip().lower()
    if mode == "english":
        mode = "romanized"
    if mode not in {"pinyin", "romanized"}:
        mode = "pinyin"

    if mode == "romanized" and slug_map:
        cat_map = slug_map.get(category, {})
        if text in cat_map:
            return cat_map[text]

    pinyin_str = pinyin_slug(text)

    if mode == "pinyin":
        return pinyin_str

    return f"romanized_{pinyin_str}"


def calculate_char_overlap_ratio(str1: str, str2: str) -> float:
    """计算两个中文实体名称的字符重合度比例。
    
    使用双向重合度（交集字符在较短串中的占比或两串平均占比）。
    针对实体别名匹配（如 'X之指' vs 'X的手指'），以最大包含度为准：
    overlap_ratio = len(chars1 & chars2) / min(len(chars1), len(chars2))
    """
    s1 = re.sub(r'[\s·・_—\-\(\)（）]', '', str1)
    s2 = re.sub(r'[\s·・_—\-\(\)（）]', '', str2)
    if not s1 or not s2:
        return 0.0
    set1, set2 = set(s1), set(s2)
    common = set1 & set2
    min_len = min(len(set1), len(set2))
    if min_len == 0:
        return 0.0
    return len(common) / min_len


CHARACTER_TITLE_SUFFIXES = ("老师", "同学", "前辈", "学长", "学姐", "会长", "校长", "爷爷", "奶奶", "队长", "医生", "大人")
FAMILY_RELATION_SUFFIXES = ("母亲", "妈妈", "父亲", "爸爸", "爷爷", "奶奶", "姐姐", "妹妹", "哥哥", "弟弟")
SUBORDINATE_RELATION_SUFFIXES = (
    "母亲", "妈妈", "父亲", "爸爸", "爷爷", "奶奶", "姐姐", "妹妹", "哥哥", "弟弟",
    "室友", "同屋", "朋友", "好友", "同学", "同事", "老师", "学生", "前男友", "前女友", "女友", "男友"
)

# --- 实体对齐硬性拒绝规则（pre-filter）与候选闸门 ---------------------------
# 设计目标：在「确定性合并规则 can_merge_*」「Rule 2.1 子串自动合并」与
# 「LLM 语义裁决」三条通道的**最前面**拦掉确定不属于同一实体的名称对，
# 命中即判 False（既不合并、也不送 LLM），从源头缩小候选池。

GENERIC_TOKEN_SUFFIXES = (
    "的房间", "之家", "术式", "咒物", "特级", "形态", "模式", "版本", "系列",
    "房间", "卧室", "母亲", "父亲", "姐姐", "妹妹", "哥哥", "弟弟", "的", "之",
)

RELATION_FORM_SUFFIXES = SUBORDINATE_RELATION_SUFFIXES + (
    "形态", "模式", "版本", "型", "式", "化身",
)

# 泛称词：仅当较短名是这些「通称」时，才判定「泛称 vs 括号限定名」应拒绝合并
GENERIC_BASE_TERMS = (
    "咒物", "咒具", "术式", "领域", "契约", "组织", "家族", "学校", "医院", "公司",
    "武器", "能力", "物品", "地点", "概念", "角色", "特级", "一级", "二级", "三级", "四级",
)

DOT_SEPARATORS = ("·", "・", "•")

_NUM_TOKEN_RE = re.compile(r"[0-9０-９]+")
_DATE_TOKEN_RE = re.compile(r"[0-9０-９]{2,4}\s*[年月日号]")
_PUNCT_VARIANT_RE = re.compile(r'[\s·・•_—\-－ー\(\)（）\[\]【】「」『』,，.。、:：;；!！?？"\'“”‘’~～]')


def _strip_name_noise(name: str) -> str:
    """去除分隔符与装饰性标点，仅保留实体名的主体字符。"""
    return _PUNCT_VARIANT_RE.sub("", str(name or ""))


def _digit_signature(name: str) -> tuple:
    """提取名称中的日期/数值签名，用于「数值或日期不同则绝不合并」的硬拒绝。

    含日期时以日期为准，否则取所有数值 token：
    '2008年' -> ('2008年',)；'数量100' -> ('100',)；'两面宿傩的手指' -> ()。
    """
    raw = _strip_name_noise(name)
    dates = tuple(_DATE_TOKEN_RE.findall(raw))
    if dates:
        return dates
    return tuple(_NUM_TOKEN_RE.findall(raw))


def _edit_distance_le1(a: str, b: str) -> bool:
    """判断两串编辑距离是否 <= 1（用于 'X·A' 与 'X·B' 的异体字豁免）。"""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    short, long_ = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long_):
        if short[i] != long_[j]:
            if skipped:
                return False
            skipped = True
            j += 1
            continue
        i += 1
        j += 1
    return True


_PINYIN_CACHE: dict = {}


def _pinyin_signature(name: str) -> tuple:
    """取名称的无调拼音签名（用于繁简/异体/形近字豁免）。

    pypinyin 已是项目依赖；不可用时返回空元组，调用方退化为纯字面判定。
    """
    key = str(name or "")
    if not key:
        return ()
    if key in _PINYIN_CACHE:
        return _PINYIN_CACHE[key]
    try:
        from pypinyin import lazy_pinyin  # 局部导入：缺失时不影响对齐主流程

        sig = tuple(lazy_pinyin(key))
    except Exception:  # noqa: BLE001
        sig = ()
    _PINYIN_CACHE[key] = sig
    return sig


def _is_homophone_variant(a: str, b: str) -> bool:
    """繁简/异体/形近字豁免：两侧无调拼音签名相同（如 钉子/釘子、锤子/槌子）。"""
    sa, sb = _pinyin_signature(a), _pinyin_signature(b)
    return bool(sa) and sa == sb


def _is_subsequence(shorter: str, longer: str) -> bool:
    """判断 shorter 是否为 longer 的字序子序列（'浮亏' ⊂ '浮动亏损'，但 '手机' ⊄ '手指'）。"""
    if not shorter or not longer or len(shorter) > len(longer):
        return False
    it = iter(longer)
    return all(ch in it for ch in shorter)


def _is_honorific_variant(name1: str, name2: str) -> bool:
    """尊称/头衔差异（'高木' vs '高木老师'）：属合法别名通道，不适用从属红线。"""
    for a, b in ((name1, name2), (name2, name1)):
        for suffix in CHARACTER_TITLE_SUFFIXES:
            if a.endswith(suffix) and not b.endswith(suffix):
                prefix = a[: -len(suffix)].rstrip("的の ")
                if prefix and b == prefix:
                    return True
    return False


def _dot_segments(name: str) -> list:
    """按层级分隔符切分名称（'机构·楼栋·房间' -> ['机构','楼栋','房间']）。"""
    raw = str(name or "")
    for sep in DOT_SEPARATORS:
        raw = raw.replace(sep, "\x00")
    return [seg for seg in raw.split("\x00") if seg]


def _has_parenthetical_qualifier(shorter: str, longer: str) -> bool:
    """判断 longer 是否为 shorter 追加括号/点号限定语（'特级咒物' vs '特级咒物（手指）'）。"""
    m = re.match(r"^(.*?)[\(（]([^\)）]+)[\)）]\s*$", str(longer or ""))
    if m:
        return _strip_name_noise(m.group(1)) == _strip_name_noise(shorter)
    segs = _dot_segments(longer)
    if len(segs) >= 2:
        head = "".join(segs[:-1])
        return _strip_name_noise(head) == _strip_name_noise(shorter)
    return False


def _strip_generic_tokens(name: str) -> str:
    """剥离高频泛词/虚词后缀，得到用于重合度计算的核心字（'X的房间' -> 'X'）。"""
    raw = _strip_name_noise(name)
    core = raw
    changed = True
    while changed and core:
        changed = False
        for tok in GENERIC_TOKEN_SUFFIXES:
            if len(core) > len(tok) and core.endswith(tok):
                core = core[: -len(tok)]
                changed = True
                break
    return core or raw


def hard_reject_pair(category: str, name1: str, name2: str):
    """硬性拒绝规则：命中返回规则名，未命中返回 None。

    命中语义 = 「确定不是同一实体」：既不合并，也不送 LLM 裁决。
    以下规则对所有类别生效，且必须先于 can_merge_*、Rule 2.1 自动合并与 LLM 粗筛执行：
      R1 数值/日期不等        —— '100' vs '200'、'2008年' vs '2009年'
      R2 'X·A' vs 'X·B'      —— 末级分支不同（A/B 编辑距离 <=1 或仅标点异体差异时豁免，交 LLM）
      R3 单侧关系/形态词      —— 'X' vs 'X母亲'、'<体系>' vs '<体系>形态'
      R4 泛称 vs 括号限定名   —— '特级咒物' vs '特级咒物（手指）'
    """
    n1 = _strip_name_noise(name1)
    n2 = _strip_name_noise(name2)
    if not n1 or not n2 or n1 == n2:
        return None

    # R1 数值/日期不等
    d1, d2 = _digit_signature(name1), _digit_signature(name2)
    if d1 and d2 and d1 != d2:
        return "R1_numeric_or_date_mismatch"

    # R2 'X·A' vs 'X·B'
    # R2 'X·A' vs 'X·B'：仅在「体系分支互斥」语义成立的类目生效。
    # 实测（3013 对缓存回放）：189 次命中里 183 次来自 locations，且其中 9 次是模型判真的
    # 同一地点同义细分（久留美家·个人房间 vs 久留美家·卧室）；abilities 命中 3 次全部为真拒绝。
    # 因此把 R2 限定在 abilities/concepts，避免用「末级分支不同」误杀地名同义写法。
    seg1, seg2 = _dot_segments(name1), _dot_segments(name2)
    if category in ("abilities", "concepts") and len(seg1) >= 2 and len(seg2) >= 2:
        head1 = _strip_name_noise("".join(seg1[:-1]))
        head2 = _strip_name_noise("".join(seg2[:-1]))
        tail1 = _strip_name_noise(seg1[-1])
        tail2 = _strip_name_noise(seg2[-1])
        if head1 == head2 and tail1 != tail2:
            # 豁免：编辑距离 <=1（须两侧均 >=2 字，否则单字分支「解/捌」会被误当异体字）
            # 或同音异体（繁简/异体字，如 釘/钉、儸/傩 视拼音而定）
            if _is_homophone_variant(tail1, tail2):
                return None
            if min(len(tail1), len(tail2)) >= 2 and _edit_distance_le1(tail1, tail2):
                return None
            return "R2_hierarchical_branch_mismatch"

    # R3 单侧关系/形态词（所有类别）
    if category == "characters":
        if is_character_subordinate_or_rel_blocked(name1, name2) and not _is_honorific_variant(name1, name2):
            return "R3_subordinate_relation"
    else:
        for a, b in ((name1, name2), (name2, name1)):
            for suffix in RELATION_FORM_SUFFIXES:
                if a.endswith(suffix) and not b.endswith(suffix):
                    prefix = a[: -len(suffix)].rstrip("的の ")
                    if prefix and (b == prefix or b.endswith(prefix) or prefix.endswith(b)):
                        return "R3_subordinate_relation"
                    break

    # R4 泛称 vs 括号限定名
    shorter, longer = (name1, name2) if len(n1) <= len(n2) else (name2, name1)
    if _has_parenthetical_qualifier(shorter, longer):
        core = _strip_generic_tokens(shorter)
        if core and any(core == term or core.endswith(term) for term in GENERIC_BASE_TERMS):
            return "R4_generic_vs_qualified"

    return None


def is_judge_candidate(category: str, name1: str, name2: str, threshold: float = 0.40) -> bool:
    """LLM 粗筛闸门（统一入口）。

    1. 先过硬性拒绝规则（命中直接 False）；
    2. 剥离泛词后缀后用核心字算重合度；
    3. 短名保护：核心字最少一侧长度 <= 2 时，禁止使用浮点阈值，
       必须满足完整子串包含，避免「两面宿傩的手指 || 手机」这类单字碰撞进候选。
    """
    if not name1 or not name2 or name1 == name2:
        return False
    if hard_reject_pair(category, name1, name2):
        return False
    c1 = _strip_generic_tokens(name1)
    c2 = _strip_generic_tokens(name2)
    if not c1 or not c2:
        return False
    if min(len(c1), len(c2)) <= 2:
        # 短名保护：不允许用浮点阈值（避免「两面宿傩的手指 ‖ 手机」这类单字碰撞）。
        # 但保留两条确定性豁免：字序子序列（浮亏 ⊂ 浮动亏损）与同音异体（钉子/釘子）。
        return _is_subsequence(c1, c2) or _is_subsequence(c2, c1) or _is_homophone_variant(c1, c2)
    return calculate_char_overlap_ratio(c1, c2) >= threshold


def is_character_subordinate_or_rel_blocked(name1: str, name2: str) -> bool:
    """防从属/亲属跨角色合并安全红线（如“A”与“A母亲”、“B”与“B的室友”严禁合并）。"""
    for s in SUBORDINATE_RELATION_SUFFIXES:
        if name1.endswith(s) and not name2.endswith(s):
            pre = name1[:-len(s)]
            if pre.endswith("的"):
                pre = pre[:-1]
            if pre:
                # 检查 name2 是否等于前缀，或以该前缀结尾，或前缀以 name2 结尾，或去掉常见外国名·分隔符后匹配
                clean_n2 = name2.split("·")[0] if "·" in name2 else name2
                if name2 == pre or name2.endswith(pre) or pre.endswith(name2) or clean_n2 == pre:
                    return True
        if name2.endswith(s) and not name1.endswith(s):
            pre = name2[:-len(s)]
            if pre.endswith("的"):
                pre = pre[:-1]
            if pre:
                clean_n1 = name1.split("·")[0] if "·" in name1 else name1
                if name1 == pre or name1.endswith(pre) or pre.endswith(name1) or clean_n1 == pre:
                    return True
    return False


def can_merge_character_names(name1: str, name2: str) -> bool:
    """判断两个角色名称是否允许归并。

    规则与安全边界：
    1. 跨角色安全红线：若其中一个是'X的关系/从属称谓'（如'X母亲'/'X室友'），另一个是该角色本身，严禁合并！
    2. 关系代称自动归并：如 'X母亲' / 'X的母亲' / '母亲' 归并为同一实体。
    3. 称谓别名自动归并：如 'X' 与 'X老师'。
    4. 全名与简称自动归并：如 '姓氏+名' 与 '名'（姓氏长1~2字，后半部分完全一致）。
    5. 禁止仅凭同姓或部分重合就合并不同角色（共享同一姓氏但名字不同的两个角色）。
    """
    if name1 == name2:
        return True

    # 1. 跨角色安全红线（亲属与从属称谓拦截）
    if is_character_subordinate_or_rel_blocked(name1, name2):
        return False

    def parse_rel(n: str):
        for s in FAMILY_RELATION_SUFFIXES:
            if n.endswith(s):
                prefix = n[:-len(s)]
                if prefix.endswith("的"):
                    prefix = prefix[:-1]
                return prefix, s
        return None, None

    base1, rel1 = parse_rel(name1)
    base2, rel2 = parse_rel(name2)

    # 2. 关系代称自动归并（如 X母亲 / X的母亲 / 母亲）
    if rel1 and rel2:
        is_same_rel = (rel1 in ("母亲", "妈妈") and rel2 in ("母亲", "妈妈")) or \
                      (rel1 in ("父亲", "爸爸") and rel2 in ("父亲", "爸爸")) or \
                      (rel1 == rel2)
        if is_same_rel:
            if not base1 or not base2:
                return True
            if base1 == base2 or base1.endswith(base2) or base2.endswith(base1):
                return True

    # 3. 称谓别名（如 X 与 X老师）
    shorter, longer = (name1, name2) if len(name1) <= len(name2) else (name2, name1)
    for suffix in CHARACTER_TITLE_SUFFIXES:
        if longer == shorter + suffix:
            return True

    # 4. 全名与简称自动归并（如 姓氏+名 与 名）
    if not rel1 and not rel2:
        if len(shorter) >= 2 and longer.endswith(shorter):
            surname_len = len(longer) - len(shorter)
            if 1 <= surname_len <= 2:
                return True

    return False


def can_merge_ability_names(name1: str, name2: str) -> bool:
    """判断两个能力名称是否允许归并。
    
    带有分支后缀的能力，核心名不同禁止合并：
    - '<体系>·<分支A>' 和 '<体系>·<分支B>' -> 禁止合并 (分支名不同)
    - '<体系>·<分支>' 和 '<体系>·<分支>（黑）' -> 允许合并 (核心名一致)
    """
    if name1 == name2:
        return True
    core1 = name1.split("·")[-1] if "·" in name1 else name1
    core2 = name2.split("·")[-1] if "·" in name2 else name2
    clean1 = re.sub(r'[\(（].*?[\)）]', '', core1).strip()
    clean2 = re.sub(r'[\(（].*?[\)）]', '', core2).strip()
    return clean1 == clean2 and bool(clean1)


def can_merge_location_names(name1: str, name2: str) -> bool:
    """判断两个地点名称是否允许归并。
    
    包含层级分隔符 · 的地点，必须比较最末级具体位置名称，禁止仅凭上级机构名重合就合并：
    - '<机构>·<地点A>' 和 '<机构>·<地点B>' -> 禁止合并 (末级名称不同)
    - '<机构>·<楼栋>走廊' 和 '<机构>·<楼栋>4楼走廊' -> 允许合并 (末级名称高度相似 >= 60%)
    - '<机构A>' 和 '<机构B>·<地点>' -> 禁止合并 (上级机构不同)
    """
    if name1 == name2:
        return True
    if "·" in name1 and "·" in name2:
        parts1 = name1.split("·")
        parts2 = name2.split("·")
        org1, detail1 = "·".join(parts1[:-1]), parts1[-1]
        org2, detail2 = "·".join(parts2[:-1]), parts2[-1]
        if org1 != org2:
            return False
        return calculate_char_overlap_ratio(detail1, detail2) >= 0.60
    return False


def can_merge_item_names(name1: str, name2: str) -> bool:
    """判断两个物品名称是否允许归并（仅保留确定性规则）。

    规则只剩两条：
    1. 名称完全一致 -> True
    2. 剥离括号后一致 -> True
    其余情形一律交给 align_entities 里的 LLM 语义裁决（overlap >= 0.40 粗筛）。
    """
    if name1 == name2:
        return True
    clean1 = re.sub(r'[\(（].*?[\)）]', '', name1).strip()
    clean2 = re.sub(r'[\(（].*?[\)）]', '', name2).strip()
    if clean1 == clean2 and clean1:
        return True
    return False


def can_merge_concept_names(name1: str, name2: str, type1: str = '', type2: str = '') -> bool:
    """判断两个概念是否允许归并（仅保留确定性规则）。

    规则只剩两条：
    1. 名称完全一致 -> True
    2. 剥离括号后一致 -> True
    其余情形一律交给 align_entities 里的 LLM 语义裁决（overlap >= 0.40 粗筛），
    由 LLM 负责区分数值/日期/汇率/OCR 变体，不再使用手工正则。
    """
    if name1 == name2:
        return True
    clean1 = re.sub(r'[\(（].*?[\)）]', '', name1).strip()
    clean2 = re.sub(r'[\(（].*?[\)）]', '', name2).strip()
    if clean1 == clean2 and clean1:
        return True
    return False


class LLMBatchJudge:
    """实体对齐语义裁决器的调用入口：按 Insight chat 配置构造 ChatClient。

    逐对/逐批裁决已由 `entity_alignment` 的「候选图 + 批量三态裁决」取代；
    这里只保留客户端构造与候选键工具，避免长期并存两套实现。
    **不内置任何网关地址与默认密钥**：未配置时 `client` 为 None，本轮不做语义合并。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        provider: str = "gemini",
        book_id: str | None = None,
    ):
        resolved_base_url = (base_url or "").strip()
        resolved_api_key = (api_key or "").strip()
        resolved_model = (model or "").strip()
        # 绝不内置网关地址或默认密钥（发布版安全要求）：缺任一关键项即视为「未配置裁决器」。
        self.enabled = bool(resolved_base_url and resolved_api_key and resolved_model)
        if self.enabled:
            from src.core.manga_insight.embedding_client import ChatClient, ChatLLMConfig
            cfg = ChatLLMConfig(
                provider=provider,
                api_key=resolved_api_key,
                model=resolved_model,
                base_url=resolved_base_url,
            )
            self.client: Any = ChatClient(cfg)
        else:
            # 未配置：本轮不做语义合并（未进图的实体仍各自保留），不发起任何请求。
            self.client = None
            LOGGER.warning(
                "实体对齐语义裁决器未配置（缺少 base_url/api_key/model），本轮跳过 LLM 语义裁决"
            )
        self.book_id = book_id

    @staticmethod
    def make_key(category: str, name1: str, name2: str) -> str:
        s1, s2 = sorted([name1, name2])
        return f"{category}::{s1}::{s2}"


class AlignmentJudgeCache:
    """实体对齐裁决结果的持久缓存（按书隔离、按请求内容寻址）。

    实体对齐的请求是纯函数：候选图与提示词版本相同，LLM 应当返回同一个裁决。
    没有缓存时，同一本书每重建一次就重新支付一遍全部裁决 token（审计管线 A）。

    缓存键只含「会被模型看到的东西」与「会改变答案的东西」：provider / model /
    base_url，以及 api_key 的短指纹——换 key 往往意味着换配额与账号策略，答案可能
    不同，但也不能把密钥明文写进磁盘。书内请求按 prompt 内容哈希寻址，所以新增或
    删除实体只影响变化的那几个分片。

    写入走「临时文件 + os.replace」，进程被杀时不会留下半个 JSON；任何读失败都视为
    未命中，绝不让缓存挡在正确性前面。
    """

    __version__ = 1

    def __init__(
        self,
        *,
        client: Any,
        path: Path,
        scope: tuple[str, ...],
    ) -> None:
        self.client = client
        self.path = path
        self.scope = scope
        self.hits = 0
        self.misses = 0
        self._entries: dict[str, Any] = {}
        self._dirty = False
        self._load()

    @staticmethod
    def default_path(data_root: Path | str | None, book_id: str | None) -> Path | None:
        if data_root is None or not book_id:
            return None
        return Path(data_root) / f"llm_entity_judge_cache.{book_id}.json"

    @staticmethod
    def _fingerprint(secret: str) -> str:
        if not secret:
            return ""
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            LOGGER.warning("实体裁决缓存不可读，本轮跳过缓存：%s", self.path, exc_info=True)
            return
        try:
            document = json.loads(raw)
        except ValueError:
            LOGGER.warning("实体裁决缓存损坏，本轮忽略并重建：%s", self.path)
            return
        if not isinstance(document, dict) or document.get("version") != self.__version__:
            return
        if tuple(document.get("scope") or ()) != self.scope:
            # 换了供应商或模型：旧裁决不再代表「同一模型的答案」，直接丢弃。
            return
        entries = document.get("entries")
        if isinstance(entries, dict):
            self._entries = dict(entries)

    def _persist(self) -> None:
        if not self._dirty:
            return
        document = {
            "version": self.__version__,
            "scope": list(self.scope),
            "entries": self._entries,
        }
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            LOGGER.warning("实体裁决缓存写入失败（不影响本轮结果）：%s", self.path, exc_info=True)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self._dirty = False

    @staticmethod
    def _key(prompt: str, system: str | None) -> str:
        digest = hashlib.sha256()
        digest.update(b"entity-alignment/1\x00")
        digest.update((system or "").encode("utf-8"))
        digest.update(b"\x00")
        digest.update(prompt.encode("utf-8"))
        return digest.hexdigest()

    async def generate_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        validator: Any = None,
    ) -> Any:
        key = self._key(prompt, system)
        cached = self._entries.get(key)
        if isinstance(cached, dict) and "payload" in cached:
            self.hits += 1
            payload = cached["payload"]
            return validator(payload) if validator is not None else payload
        self.misses += 1
        payload = await self.client.generate_json(
            prompt=prompt,
            system=system,
            validator=validator,
        )
        # 只缓存成功结果：失败的请求必须下次真的重问，而不是缓存成一个坏答案。
        self._entries[key] = {"payload": payload}
        self._dirty = True
        return payload

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "entries": len(self._entries)}

    def close(self) -> None:
        self._persist()


def _alignment_progress(message: str, done: int | None, total: int | None) -> None:
    """把实体对齐阶段的进度写进任务日志（该阶段过去对用户完全不可见）。"""
    if done is None or total is None:
        log_result(message, ("正在准备候选对…",))
    else:
        log_result(message, (f"已完成裁决请求 {done}/{total}",))

def check_model_entity_similarity(
    name1: str,
    name2: str,
    category: str,
    *,
    llm_callable: Callable[[str], str] | None = None,
) -> bool:
    """调用轻量模型辅助判定两个重合度在 40%-60% 之间的模糊实体是否为同一实体。"""
    if llm_callable is None:
        return False
    prompt = (
        f"请判断在漫画领域中，类别为【{category}】的以下两个名称是否指代同一个实体：\n"
        f"名称1：{name1}\n"
        f"名称2：{name2}\n"
        f"如果是同一个实体请仅回复 'yes'，如果不是请仅回复 'no'。"
    )
    try:
        reply = llm_callable(prompt).strip().lower()
        return "yes" in reply
    except Exception:
        return False


def _normalize_layer_events(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("summary layer key_events must be an array")
    events = []
    for index, raw in enumerate(value):
        # Earlier layer prompts also accepted plain event descriptions.
        event = {"summary": raw} if isinstance(raw, str) else raw
        if not isinstance(event, Mapping):
            raise ValueError(f"summary layer key_events[{index}] must be an object or text")
        summary = event.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(f"summary layer key_events[{index}].summary must be non-empty text")
        importance = event.get("importance", "normal")
        if not isinstance(importance, str) or importance not in ALLOWED_IMPORTANCE:
            raise ValueError(f"summary layer key_events[{index}].importance is invalid")
        normalized = {**event, "summary": summary.strip(), "importance": importance}
        if "event_type" in event:
            event_type = event["event_type"]
            if not isinstance(event_type, str) or not event_type.strip():
                raise ValueError(f"summary layer key_events[{index}].event_type must be non-empty text")
            normalized["event_type"] = event_type.strip()
        events.append(normalized)
    return events


def _split_entity_pages(
    pages: object,
    *,
    detached_freq: int,
) -> tuple[list[int], list[int]]:
    """按拆分频次把一个实体的出场页切给拆出的实体。

    实体上的 ``pages`` 与 ``total_frequency`` 描述的是同一批证据（见
    ``_validate_canonical_entities`` 的 ``total_frequency == len(pages)`` 约定）。
    手工拆分一个别名时，若把源实体的 ``pages`` 整份复制给新实体，两个实体会
    声明相同的出场页，图谱与列表页的共现统计随之翻倍。这里按频次切分，
    并保证两边都至少保留 1 页，使 ``total_frequency == len(pages)`` 两边都成立。

    只有 1 页证据时无法在页维度上区分：整页归拆出的实体（它是被显式指认的
    那一侧），源实体拿到空页集，调用方据此把它的频次归一到 1。
    """
    if not isinstance(pages, (list, tuple)):
        return [], []
    ordered: list[int] = []
    seen: set[int] = set()
    for value in pages:
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    if not ordered:
        return [], []
    if len(ordered) == 1:
        return list(ordered), []
    take = min(max(1, int(detached_freq)), len(ordered) - 1)
    return list(ordered[:take]), list(ordered[take:])


def _json_object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise InsightInvariant(f"stored {field} is missing")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise InsightInvariant(
            f"stored {field} is invalid"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise InsightInvariant(f"stored {field} must be an object")
    return dict(parsed)


def _required_mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InsightConflict(f"{field} must be an object")
    return dict(value)


def _required_integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InsightConflict(f"{field} must be an integer of at least {minimum}")
    return value


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InsightConflict(f"{field} must be a non-empty string")
    return value


def _required_text(value: object, field: str) -> str:
    text = _required_string(value, field)
    if not text.strip():
        raise InsightConflict(f"{field} must not be blank")
    return text


PAGE_VECTOR_ENTITY_FIELDS = (
    ("characters", "角色"),
    ("locations", "地点"),
    ("abilities", "能力"),
    ("items", "物品"),
)

# 单页向量文档的字符上限。个别页的关键事件异常密集（实测一页可达数万字），会超出
# embedding 模型（Cloudflare @cf/qwen/qwen3-embedding-0.6b）的输入上限，让整个
# 「建立语义索引」步骤以 `API 错误 400: input too big` 失败。只在超限时截断，且保留
# 文档开头（摘要 → 出场实体 → 最早的关键事件），语义检索最有用信息都在前部。
PAGE_VECTOR_DOCUMENT_MAX_CHARS = 4000

# 单个 embedding 请求的总字符上限（不是单条）。Cloudflare
# @cf/qwen/qwen3-embedding-0.6b 的上限作用在整批输入上：实测 64 条 × 1000 字
# （6.4 万字）通过，32 条 × 4000 字与 64 条 × 2000 字（12.8 万字）都返回
# `API 错误 400: input too big (code 3030)`，且上游要 70~130 秒才肯报错。
# 因此除了单页文档截断，还必须按总字数切批；留足余量取 4.8 万字。
EMBEDDING_BATCH_MAX_CHARS = 48_000


def build_page_vector_document(analysis: Mapping[str, Any], summary: str) -> str:
    """把一页的分析结果拼成语义检索用的文档。

    过去这里只放 ``page_summary``（实测 57..221 字），角色、地点、道具与关键事件
    全被丢掉，于是「某角色第一次出现在第几话」这类问题与页面摘要的语义距离很远：
    第 46 页写的是「把手指抛向口中」，问的是「第一次吃下宿傩手指」，向量排序只能
    排到第 11 名（topK=10 刚好错过）。补上结构化字段后同一问题的排名升到第 2 名。

    文档顺序固定（摘要 → 出场实体 → 关键事件），保证同一份分析永远产出同一段
    文本，向量可复现；空字段直接跳过，不给文档塞「无」这类噪声。

    超过 ``PAGE_VECTOR_DOCUMENT_MAX_CHARS`` 时按字符截断（保留前部），
    否则个别事件密集的页会撑爆 embedding 模型的输入上限。
    """
    parts: list[str] = [summary.strip()]
    for field, label in PAGE_VECTOR_ENTITY_FIELDS:
        entries = analysis.get(field)
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)):
            continue
        names: list[str] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        if names:
            parts.append(f"{label}：" + "、".join(dict.fromkeys(names)))
    events = analysis.get("key_events")
    if isinstance(events, Sequence) and not isinstance(events, (str, bytes, bytearray)):
        for event in events:
            if not isinstance(event, Mapping):
                continue
            text = event.get("summary")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    document = "\n".join(part for part in parts if part)
    if len(document) > PAGE_VECTOR_DOCUMENT_MAX_CHARS:
        document = document[:PAGE_VECTOR_DOCUMENT_MAX_CHARS]
    return document


def _required_boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise InsightConflict(f"{field} must be a boolean")
    return value


def _required_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise InsightConflict(f"{field} must be a timestamp")
    return value


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field)


def validate_artifact_payload(
    *,
    kind: str,
    template: str,
    payload: object,
) -> dict[str, Any]:
    if kind == "overview":
        if template not in OVERVIEW_TEMPLATES:
            raise InsightConflict("Insight overview template is invalid")
    elif kind == "compressed_context":
        if template != "default":
            raise InsightConflict("Insight compressed context template is invalid")
    elif kind == "entity_alignment":
        if template != "default":
            raise InsightConflict("Insight entity alignment template is invalid")
    else:
        raise InsightConflict("Insight artifact kind is invalid")
    if not isinstance(payload, Mapping) or not contains_nonempty_text(payload):
        raise InsightConflict("Insight artifact payload must be a non-empty object")
    normalized = dict(payload)
    if kind == "overview":
        _required_text(normalized.get("title"), "overview title")
        _required_text(normalized.get("content"), "overview content")
    elif kind == "entity_alignment":
        if "canonical_entities" not in normalized or not isinstance(normalized["canonical_entities"], list):
            raise InsightConflict("entity_alignment payload must contain canonical_entities list")
        if "alias_map" not in normalized or not isinstance(normalized["alias_map"], dict):
            raise InsightConflict("entity_alignment payload must contain alias_map dictionary")
        # 兼容新格式 auto_merge_log 与历史格式 pending_review_queue
        if "auto_merge_log" not in normalized and "pending_review_queue" not in normalized:
            raise InsightConflict("entity_alignment payload must contain auto_merge_log or pending_review_queue")
    return normalized


def _required_sha256(value: object, field: str) -> str:
    text = _required_string(value, field)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise InsightConflict(f"{field} must be a lowercase SHA-256 digest")
    return text


def _persisted_page_analysis(
    value: object,
    *,
    page_id: str,
    page_number: int,
    source_asset_id: str,
    source_checksum: str,
    run_id: str | None = None,
    connection: Connection | Engine | None = None,
    extra_entities: Sequence[str] | None = None,
) -> dict[str, Any]:
    try:
        parsed = _json_object(value, "analysis page payload")
        if extra_entities is not None:
            payload = validate_persisted_page_analysis(
                parsed, extra_entities=extra_entities
            )
        elif run_id is not None and connection is not None:
            target = connection
            if hasattr(target, "closed") and target.closed:
                target = getattr(target, "engine", connection)
            payload = deserialize_page_analysis(
                parsed,
                run_id=run_id,
                target=target,
            )
        else:
            payload = validate_persisted_page_analysis(parsed)
    except InvalidPageAnalysis as exc:
        raise InsightInvariant(
            "stored analysis page payload is invalid"
        ) from exc
    if (
        payload["page_id"] != page_id
        or payload["page_number_snapshot"] != page_number
        or payload["source_asset_id"] != source_asset_id
        or payload["source_checksum"] != source_checksum
    ):
        raise InsightInvariant(
            "stored analysis page payload identity is invalid"
        )
    return payload


def _safe_timeline_error(error: object) -> str:
    message = redact_sensitive_text(error).strip()
    if not message:
        message = type(error).__name__
    return message[:1000]


def _timeline_optional_fields(value: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """Represent absent optional model values by omitted fields on the wire."""
    return {
        key: item for key, item in value.items()
        if key not in fields or (
            item is not None
            and not (key in {"arc", "importance"} and isinstance(item, str) and not item.strip())
        )
    }


def _validate_timeline_parts(
    *,
    content: object,
    events: object,
    characters: object,
    require_events: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(content, Mapping):
        raise ValueError("timeline response content must be an object")
    if not isinstance(events, list) or (require_events and not events):
        raise ValueError("timeline response must contain at least one event")
    if not isinstance(characters, list):
        raise ValueError("timeline response is missing characters")
    content = _timeline_optional_fields(content, ("plot_arcs", "plot_threads"))
    for field, optional_fields in (
        ("plot_arcs", ("mood", "event_ids")),
        ("plot_threads", ("description", "introduced_at", "resolved_at")),
    ):
        if isinstance(content.get(field), list):
            content[field] = [
                _timeline_optional_fields(item, optional_fields) if isinstance(item, Mapping) else item
                for item in content[field]
            ]

    validated_events: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise ValueError(f"timeline event {index + 1} must be an object")
        summary = event.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(f"timeline event {index + 1} summary is invalid")
        page_ids = event.get("page_ids")
        if page_ids is not None and (
            not isinstance(page_ids, list)
            or any(not isinstance(value, str) or not value for value in page_ids)
        ):
            raise ValueError(f"timeline event {index + 1} page_ids are invalid")
        page_numbers = event.get("page_numbers")
        if page_numbers is not None and (
            not isinstance(page_numbers, list)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                for value in page_numbers
            )
        ):
            raise ValueError(f"timeline event {index + 1} page_numbers are invalid")
        if not page_ids and not page_numbers:
            raise ValueError(
                f"timeline event {index + 1} must reference at least one page"
            )
        if event.get("importance") is not None and not isinstance(event["importance"], str):
            raise ValueError(f"timeline event {index + 1} importance must be a string")
        validated_events.append(_timeline_optional_fields(event, ("importance",)))

    validated_characters: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, character in enumerate(characters):
        if not isinstance(character, Mapping):
            raise ValueError(f"timeline character {index + 1} must be an object")
        name = character.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"timeline character {index + 1} name is invalid")
        if name in names:
            raise ValueError(f"timeline character name is duplicated: {name}")
        names.add(name)
        description = character.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(
                f"timeline character {index + 1} description is invalid"
            )
        if character.get("arc") is not None and not isinstance(character["arc"], str):
            raise ValueError(f"timeline character {index + 1} arc must be a string")
        first_page = character.get("first_page")
        if (
            isinstance(first_page, bool)
            or not isinstance(first_page, int)
            or first_page < 1
        ):
            raise ValueError(f"timeline character {index + 1} first_page is invalid")
        related_pages = character.get("related_page_numbers")
        if related_pages is not None and (
            not isinstance(related_pages, list)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                for value in related_pages
            )
        ):
            raise ValueError(
                f"timeline character {index + 1} related_page_numbers are invalid"
            )
        key_moments = character.get("key_moments")
        if not isinstance(key_moments, list):
            raise ValueError(
                f"timeline character {index + 1} key_moments are invalid"
            )
        for moment_index, moment in enumerate(key_moments):
            if not isinstance(moment, Mapping):
                raise ValueError(
                    f"timeline character {index + 1} key moment "
                    f"{moment_index + 1} must be an object"
                )
            page = moment.get("page")
            summary = moment.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError(
                    f"timeline character {index + 1} key moment "
                    f"{moment_index + 1} summary is invalid"
                )
            if page is not None and (
                isinstance(page, bool)
                or not isinstance(page, int)
                or page < 1
            ):
                raise ValueError(
                    f"timeline character {index + 1} key moment "
                    f"{moment_index + 1} page is invalid"
                )
        normalized_character = _timeline_optional_fields(character, ("arc", "related_page_numbers"))
        normalized_character["key_moments"] = [
            _timeline_optional_fields(moment, ("page",)) for moment in key_moments
        ]
        validated_characters.append(normalized_character)
    return dict(content), validated_events, validated_characters


def _normalized_timeline_result(
    result: object,
    *,
    mode: str,
    fallback_reason: str | None,
) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise ValueError("timeline response must be an object")
    content, events, characters = _validate_timeline_parts(
        content=result.get("content"),
        events=result.get("events"),
        characters=result.get("characters"),
        require_events=True,
    )
    content.update(
        {
            "requested_mode": "enhanced",
            "actual_mode": mode,
            "fallback_reason": fallback_reason,
            "degraded": mode != "enhanced",
        }
    )
    try:
        _validate_timeline_metadata(content, mode=mode)
    except InsightConflict as exc:
        # Model output errors must enter the shared JSON business-retry path.
        raise ValueError(str(exc)) from exc
    return {
        "mode": mode,
        "content": content,
        "events": events,
        "characters": characters,
    }


def _validate_timeline_content_collections(content: Mapping[str, Any]) -> None:
    plot_arcs = content.get("plot_arcs")
    if plot_arcs is not None:
        if not isinstance(plot_arcs, list):
            raise InsightConflict("timeline plot_arcs must be an array")
        arc_ids: set[str] = set()
        for index, arc in enumerate(plot_arcs, start=1):
            if not isinstance(arc, Mapping):
                raise InsightConflict(f"timeline plot arc {index} must be an object")
            arc_id = _required_text(arc.get("id"), f"timeline plot arc {index} id")
            if arc_id in arc_ids:
                raise InsightConflict(f"timeline plot arc id is duplicated: {arc_id}")
            arc_ids.add(arc_id)
            _required_text(arc.get("name"), f"timeline plot arc {index} name")
            _required_text(
                arc.get("description"),
                f"timeline plot arc {index} description",
            )
            page_range = arc.get("page_range")
            if not isinstance(page_range, Mapping):
                raise InsightConflict(
                    f"timeline plot arc {index} page_range must be an object"
                )
            start = _required_integer(
                page_range.get("start"),
                f"timeline plot arc {index} start page",
                minimum=1,
            )
            end = _required_integer(
                page_range.get("end"),
                f"timeline plot arc {index} end page",
                minimum=1,
            )
            if end < start:
                raise InsightConflict(
                    f"timeline plot arc {index} page range is reversed"
                )
            for field in ("mood",):
                if arc.get(field) is not None:
                    _required_text(
                        arc.get(field),
                        f"timeline plot arc {index} {field}",
                    )
            event_ids = arc.get("event_ids")
            if event_ids is not None and (
                not isinstance(event_ids, list)
                or any(not isinstance(value, str) or not value for value in event_ids)
                or len(set(event_ids)) != len(event_ids)
            ):
                raise InsightConflict(
                    f"timeline plot arc {index} event_ids are invalid"
                )

    plot_threads = content.get("plot_threads")
    if plot_threads is None:
        return
    if not isinstance(plot_threads, list):
        raise InsightConflict("timeline plot_threads must be an array")
    thread_ids: set[str] = set()
    for index, thread in enumerate(plot_threads, start=1):
        if not isinstance(thread, Mapping):
            raise InsightConflict(f"timeline plot thread {index} must be an object")
        thread_id = _required_text(
            thread.get("id"),
            f"timeline plot thread {index} id",
        )
        if thread_id in thread_ids:
            raise InsightConflict(f"timeline plot thread id is duplicated: {thread_id}")
        thread_ids.add(thread_id)
        for field in ("name", "type", "status"):
            _required_text(
                thread.get(field),
                f"timeline plot thread {index} {field}",
            )
        if thread.get("description") is not None:
            _required_text(
                thread.get("description"),
                f"timeline plot thread {index} description",
            )
        for field in ("introduced_at", "resolved_at"):
            if thread.get(field) is not None:
                _required_integer(
                    thread.get(field),
                    f"timeline plot thread {index} {field}",
                    minimum=1,
                )


def _validate_timeline_metadata(
    content: Mapping[str, Any],
    *,
    mode: str,
) -> None:
    story_summary = content.get("story_summary")
    if not isinstance(story_summary, str):
        raise InsightConflict("timeline story_summary must be a string")
    if mode != "simple" and not story_summary.strip():
        raise InsightConflict(
            "enhanced timeline story_summary must be a non-empty string"
        )
    if content.get("requested_mode") != "enhanced":
        raise InsightConflict("timeline requested_mode is invalid")
    if content.get("actual_mode") != mode:
        raise InsightConflict("timeline actual_mode does not match mode")
    degraded = _required_boolean(content.get("degraded"), "timeline degraded")
    if degraded != (mode != "enhanced"):
        raise InsightConflict("timeline degraded flag does not match mode")
    fallback_reason = content.get("fallback_reason")
    if mode == "enhanced":
        if fallback_reason is not None:
            raise InsightConflict(
                "enhanced timeline fallback_reason must be null"
            )
    elif not isinstance(fallback_reason, str) or not fallback_reason.strip():
        raise InsightConflict(
            "degraded timeline fallback_reason must be a non-empty string"
        )
    _validate_timeline_content_collections(content)


def validate_timeline_payload(
    *,
    mode: str,
    content: object,
    events: object,
    characters: object,
    require_events: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if mode not in {"enhanced", "compressed", "simple"}:
        raise InsightConflict("timeline mode is invalid")
    try:
        validated = _validate_timeline_parts(
            content=content,
            events=events,
            characters=characters,
            require_events=require_events,
        )
    except ValueError as exc:
        raise InsightConflict("timeline payload is invalid") from exc
    _validate_timeline_metadata(validated[0], mode=mode)
    return validated


def _canonical_timeline_references(
    *,
    pages: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    characters: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if any("eventId" in event for event in events):
        raise InsightConflict("timeline event must not define eventId")
    if any("characterId" in character for character in characters):
        raise InsightConflict("timeline character must not define characterId")
    page_ids_by_number: dict[int, str] = {}
    page_numbers_by_id: dict[str, int] = {}
    for index, page in enumerate(pages, start=1):
        page_id = _required_string(
            page.get("pageId"),
            f"timeline source page {index} pageId",
        )
        page_number = _required_integer(
            page.get("pageNumber"),
            f"timeline source page {index} pageNumber",
            minimum=1,
        )
        if page_id in page_numbers_by_id or page_number in page_ids_by_number:
            raise InsightConflict("timeline source pages are duplicated")
        page_numbers_by_id[page_id] = page_number
        page_ids_by_number[page_number] = page_id

    canonical_events: list[dict[str, Any]] = []
    for index, event in enumerate(events, start=1):
        page_ids = list(event.get("page_ids") or [])
        page_numbers = list(event.get("page_numbers") or [])
        if len(set(page_ids)) != len(page_ids) or len(set(page_numbers)) != len(
            page_numbers
        ):
            raise InsightConflict(f"timeline event {index} page references are duplicated")
        if page_ids:
            if any(page_id not in page_numbers_by_id for page_id in page_ids):
                raise InsightConflict(
                    f"timeline event {index} references an unknown page"
                )
            expected_numbers = [page_numbers_by_id[page_id] for page_id in page_ids]
            if page_numbers and page_numbers != expected_numbers:
                raise InsightConflict(
                    f"timeline event {index} page references do not match"
                )
            page_numbers = expected_numbers
        else:
            if any(page_number not in page_ids_by_number for page_number in page_numbers):
                raise InsightConflict(
                    f"timeline event {index} references an unknown page"
                )
            page_ids = [page_ids_by_number[page_number] for page_number in page_numbers]
        canonical_events.append(
            {
                **dict(event),
                "page_ids": page_ids,
                "page_numbers": page_numbers,
            }
        )

    canonical_characters: list[dict[str, Any]] = []
    valid_page_numbers = set(page_ids_by_number)
    for index, character in enumerate(characters, start=1):
        referenced = [character["first_page"]]
        referenced.extend(character.get("related_page_numbers") or [])
        referenced.extend(
            moment["page"]
            for moment in character["key_moments"]
            if moment.get("page") is not None
        )
        if any(page_number not in valid_page_numbers for page_number in referenced):
            raise InsightConflict(
                f"timeline character {index} references an unknown page"
            )
        canonical_characters.append(dict(character))
    return canonical_events, canonical_characters


def _timeline_thumbnail_page_numbers(
    *,
    content: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    characters: Sequence[Mapping[str, Any]],
) -> set[int]:
    numbers: set[int] = set()

    def add(value: object, field: str) -> None:
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise InsightInvariant(
                f"stored timeline {field} is invalid"
            )
        numbers.add(value)

    for event in events:
        page_numbers = event.get("page_numbers", [])
        if not isinstance(page_numbers, list):
            raise InsightInvariant(
                "stored timeline event page_numbers are invalid"
            )
        for value in page_numbers:
            add(value, "event page number")
    for character in characters:
        add(character.get("first_page"), "character first page")
        related_pages = character.get("related_page_numbers", [])
        if not isinstance(related_pages, list):
            raise InsightInvariant(
                "stored timeline character page numbers are invalid"
            )
        for value in related_pages:
            add(value, "character related page")
        key_moments = character.get("key_moments", [])
        if not isinstance(key_moments, list):
            raise InsightInvariant(
                "stored timeline character moments are invalid"
            )
        for moment in key_moments:
            if not isinstance(moment, Mapping):
                raise InsightInvariant(
                    "stored timeline character moment is invalid"
                )
            add(moment.get("page"), "character moment page")
    plot_arcs = content.get("plot_arcs", [])
    if not isinstance(plot_arcs, list):
        raise InsightInvariant("stored timeline plot_arcs are invalid")
    for arc in plot_arcs:
        if not isinstance(arc, Mapping):
            raise InsightInvariant("stored timeline plot arc is invalid")
        page_range = arc.get("page_range")
        if page_range is not None:
            if not isinstance(page_range, Mapping):
                raise InsightInvariant(
                    "stored timeline plot arc page range is invalid"
                )
            add(page_range.get("start"), "plot arc start page")
    plot_threads = content.get("plot_threads", [])
    if not isinstance(plot_threads, list):
        raise InsightInvariant(
            "stored timeline plot_threads are invalid"
        )
    for thread in plot_threads:
        if not isinstance(thread, Mapping):
            raise InsightInvariant(
                "stored timeline plot thread is invalid"
            )
        add(thread.get("introduced_at"), "plot thread introduced page")
        add(thread.get("resolved_at"), "plot thread resolved page")
    return numbers


@dataclass(frozen=True, slots=True)
class AnalysisInputSnapshot:
    book_id: str
    source_run_id: str | None
    source_run_status: str | None
    result_ids: tuple[str, ...]
    pages: tuple[dict[str, Any], ...]
    fingerprint: str


class DerivedAlgorithms(Protocol):
    def build_layer(
        self,
        inputs: Sequence[Mapping[str, Any]],
        *,
        layer: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def build_overview(
        self,
        pages: Sequence[Mapping[str, Any]],
        *,
        template: str,
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def build_compressed_context(
        self,
        pages: Sequence[Mapping[str, Any]],
        *,
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def build_timeline(
        self,
        pages: Sequence[Mapping[str, Any]],
        *,
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def embed_documents(
        self,
        documents: Sequence[str],
        *,
        config: Mapping[str, Any],
    ) -> Sequence[Sequence[float]]: ...


class ProviderDerivedAlgorithms:
    """Current derived-analysis implementation for the Worker."""

    def build_layer(
        self,
        inputs: Sequence[Mapping[str, Any]],
        *,
        layer: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        prompt_type = _required_string(
            layer.get("promptType"),
            "Insight layer promptType",
        )
        layer_name = _required_string(layer.get("name"), "Insight layer name")
        prompt = (
            f"请生成“{layer_name}”层级摘要。"
            '只依据输入，保留关键事件、连续性和因果关系。输出一个 JSON 对象，'
            '格式为 {"summary":"本组摘要","key_events":[{"summary":"事件描述","importance":"normal"}]}。'
            'summary 必须是非空文本；key_events 中每项为事件对象，importance 仅使用 high、medium、normal，'
            '可选 event_type 为非空文本。没有关键事件时 key_events 为 []，不要输出顶层数组。\n\n'
            + "\n\n".join(_json(dict(value)) for value in inputs)
        )
        def validate(result: Any) -> dict[str, Any]:
            if not isinstance(result, Mapping) or not result:
                raise ValueError("summary layer response must be a non-empty object")
            summary = result.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("summary layer summary must be non-empty text")
            return {
                **result,
                "summary": summary.strip(),
                "key_events": _normalize_layer_events(result.get("key_events", [])),
            }

        return self._chat_json(
            prompt,
            config=config,
            prompt_type=prompt_type,
            validator=validate,
        )

    def build_overview(
        self,
        pages: Sequence[Mapping[str, Any]],
        *,
        template: str,
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        prompt = (
            f"请根据以下逐页分析生成“{template}”漫画概览。"
            "只依据输入，不补写不存在的情节。只输出一个 JSON 对象，"
            '格式为 {"title":"非空标题字符串","content":"非空 Markdown 正文字符串"}。'
            "title 与 content 必须直接位于顶层，值均为非空字符串；不要用模板名称包裹正文，"
            "不要把 content 写成对象或数组。\n\n"
            + _page_context(pages)
        )
        return self._chat_json(
            prompt, config=config, prompt_type="book_overview",
            validator=lambda result: validate_artifact_payload(
                kind="overview", template=template, payload=result,
            ),
        )

    def build_compressed_context(
        self,
        pages: Sequence[Mapping[str, Any]],
        *,
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        prompt = (
            "把以下漫画逐页分析压缩成可供后续问答和剧情生成使用的上下文。"
            "保留事件顺序、因果、角色状态变化和未解决线索。输出包含实质文本内容的 JSON 对象。\n\n"
            + _page_context(pages)
        )
        return self._chat_json(
            prompt, config=config, prompt_type="group_summary",
            validator=lambda result: validate_artifact_payload(
                kind="compressed_context", template="default", payload=result,
            ),
        )

    def build_timeline(
        self,
        pages: Sequence[Mapping[str, Any]],
        *,
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        # Summary units and compressed context can cover overlapping pages.
        source_pages: dict[str, dict[str, Any]] = {}
        for page in pages:
            _page_context_label(page)
            ids = page["pageIds"] if page.get("pageIds") is not None else [page["pageId"]]
            numbers = page["pageNumbers"] if page.get("pageNumbers") is not None else [page["pageNumber"]]
            for page_id, number in zip(ids, numbers, strict=True):
                source = {"pageId": page_id, "pageNumber": number}
                if page_id in source_pages and source_pages[page_id] != source:
                    raise InsightConflict("timeline source page references do not match")
                source_pages[page_id] = source

        def validate(result: object, *, mode: str, fallback_reason: str | None) -> dict[str, Any]:
            normalized = _normalized_timeline_result(
                result, mode=mode, fallback_reason=fallback_reason,
            )
            normalized["events"], normalized["characters"] = _canonical_timeline_references(
                pages=list(source_pages.values()),
                events=normalized["events"], characters=normalized["characters"],
            )
            return normalized

        output_contract = (
            "只输出 JSON 对象，格式为："
            '{"content":{"story_summary":"...","plot_arcs":'
            '[{"id":"...","name":"...","description":"...",'
            '"page_range":{"start":1,"end":2}}],'
            '"plot_threads":[{"id":"...","name":"...",'
            '"type":"伏笔","status":"未解决"}]},'
            '"events":[{"summary":"...","page_ids":["..."]}],'
            '"characters":[{"name":"...","aliases":[],"description":"...",'
            '"personality":"...","arc":"...","first_page":1,'
            '"key_moments":[{"summary":"...","page":1}],'
            '"related_page_numbers":[1]}]}。'
            "story_summary 必须是非空文本；plot_arcs 中每项必须包含 id、name、description 和 page_range，"
            "plot_threads 中每项必须包含非空文本 id、name、type、status，各自数组内的 id 不得重复。"
            "剧情线可选 description 为非空文本，introduced_at 为正整数页码，resolved_at 为正整数页码或 null。"
            "没有可确认的剧情阶段或剧情线时使用空数组，不要为了填充字段编造事实。"
            "events 至少包含一个有来源的事件，使用输入中实际的 page_ids 或 page_numbers 关联页面；"
            "页码必须为正整数，page_range.end 不得小于 start。"
            "没有角色资料时 characters 为 []。不要输出系统生成的 eventId、characterId 或模式元数据。"
            "不要把推断写成事实。\n\n"
        )
        enhanced_prompt = (
            "根据以下漫画分析生成增强时间线。" + output_contract
            + _page_context(pages)
        )
        enhanced_error: Exception | None = None
        try:
            return self._chat_json(
                enhanced_prompt,
                config=config,
                prompt_type="book_overview",
                validator=lambda result: validate(
                    result, mode="enhanced", fallback_reason=None,
                ),
            )
        except Exception as exc:
            if is_memory_allocation_error(exc):
                raise
            enhanced_error = exc

        compressed_inputs: list[Mapping[str, Any]] = []
        for index, page in enumerate(pages):
            analysis = _required_mapping(
                page.get("analysis"),
                f"timeline input {index + 1} analysis",
            )
            compressed_context = analysis.get("compressed_context")
            if compressed_context is None:
                continue
            compressed_payload = _required_mapping(
                compressed_context,
                f"timeline input {index + 1} compressed_context",
            )
            if compressed_payload:
                compressed_inputs.append(page)
        compressed_error: Exception | None = None
        if compressed_inputs:
            compressed_prompt = (
                "根据以下压缩上下文生成漫画时间线。" + output_contract
                + _page_context(compressed_inputs)
            )
            try:
                return self._chat_json(
                    compressed_prompt,
                    config=config,
                    prompt_type="book_overview",
                    validator=lambda result: validate(
                        result, mode="compressed",
                        fallback_reason=_safe_timeline_error(enhanced_error),
                    ),
                )
            except Exception as exc:
                if is_memory_allocation_error(exc):
                    raise
                compressed_error = exc

        events = []
        for page_index, page in enumerate(pages):
            payload = _required_mapping(
                page.get("analysis"),
                f"timeline input {page_index + 1} analysis",
            )
            raw_events = payload.get("key_events", [])
            if not isinstance(raw_events, list):
                raise InsightConflict(
                    f"timeline input {page_index + 1} key_events must be an array"
                )
            raw_page_ids = page.get("pageIds")
            if raw_page_ids is None:
                raw_page_ids = [page.get("pageId")]
            if not isinstance(raw_page_ids, list) or any(
                not isinstance(value, str) or not value for value in raw_page_ids
            ):
                raise InsightConflict(
                    f"timeline input {page_index + 1} pageIds are invalid"
                )
            raw_page_numbers = page.get("pageNumbers")
            if raw_page_numbers is None:
                raw_page_numbers = [page.get("pageNumber")]
            if not isinstance(raw_page_numbers, list) or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                for value in raw_page_numbers
            ):
                raise InsightConflict(
                    f"timeline input {page_index + 1} pageNumbers are invalid"
                )
            for event_index, event in enumerate(raw_events):
                if not isinstance(event, Mapping):
                    raise InsightConflict(
                        f"timeline input {page_index + 1} event "
                        f"{event_index + 1} must be an object"
                    )
                summary = event.get("summary")
                importance = event.get("importance", "normal")
                if not isinstance(summary, str) or not summary.strip():
                    raise InsightConflict(
                        f"timeline input {page_index + 1} event "
                        f"{event_index + 1} summary is invalid"
                    )
                if not isinstance(importance, str):
                    raise InsightConflict(
                        f"timeline input {page_index + 1} event "
                        f"{event_index + 1} importance is invalid"
                    )
                events.append(
                    {
                        "summary": summary,
                        "importance": importance,
                        "page_ids": list(raw_page_ids),
                        "page_numbers": list(raw_page_numbers),
                    }
                )
        if not events:
            reasons = [f"enhanced: {_safe_timeline_error(enhanced_error)}"]
            if compressed_error is not None:
                reasons.append(
                    f"compressed: {_safe_timeline_error(compressed_error)}"
                )
            raise InsightConflict(
                "timeline generation failed in every mode; " + "; ".join(reasons)
            )
        fallback_reason = _safe_timeline_error(enhanced_error)
        if compressed_error is not None:
            fallback_reason += (
                f"; compressed: {_safe_timeline_error(compressed_error)}"
            )
        return {
            "mode": "simple",
            "content": {
                "story_summary": "\n".join(event["summary"] for event in events),
                "requested_mode": "enhanced",
                "actual_mode": "simple",
                "fallback_reason": fallback_reason,
                "degraded": True,
                "source": "page_key_events",
            },
            "events": events,
            "characters": [],
        }

    def embed_documents(
        self,
        documents: Sequence[str],
        *,
        config: Mapping[str, Any],
    ) -> Sequence[Sequence[float]]:
        from src.core.manga_insight.embedding_client import EmbeddingClient

        client = EmbeddingClient(frozen_embedding_config(config))

        async def execute() -> Sequence[Sequence[float]]:
            return await client.embed_batch(list(documents))

        return asyncio.run(execute())

    @staticmethod
    def _chat_json(
        prompt: str,
        *,
        config: Mapping[str, Any],
        prompt_type: str,
        validator: Callable[[Any], Any] | None = None,
    ) -> object:
        from src.core.manga_insight.embedding_client import ChatClient

        chat_config = frozen_chat_config(config)
        prompts = _required_mapping(
            config.get("prompts"),
            "frozen Insight prompts",
        )
        system_section = _required_mapping(
            prompts.get("analysis_system"),
            "frozen Insight analysis_system prompt",
        )
        prompt_section = _required_mapping(
            prompts.get(prompt_type),
            f"frozen Insight {prompt_type} prompt",
        )
        system = _required_string(
            system_section.get("content"),
            "frozen Insight analysis_system prompt content",
        )
        configured = _required_string(
            prompt_section.get("content"),
            f"frozen Insight {prompt_type} prompt content",
        )
        client = ChatClient(chat_config)

        async def execute() -> object:
            def validate(result: Any) -> Any:
                try:
                    return validator(result) if validator is not None else result
                except InsightConflict as exc:
                    raise ValueError(str(exc)) from exc

            return await client.generate_json(
                f"{configured}\n\n{prompt}".strip(),
                system=system,
                validator=validate,
            )

        return asyncio.run(execute())


def _validated_vector_batch(
    records: Sequence[Mapping[str, Any]],
    embeddings: Sequence[Sequence[float]],
    *,
    field: str,
    expected_dimension: int | None,
) -> tuple[
    list[str],
    list[str],
    list[dict[str, str | int | float | bool]],
    list[list[float]],
    int,
]:
    if not records or len(records) != len(embeddings):
        raise InsightConflict(f"{field} embedding result count mismatch")
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict[str, str | int | float | bool]] = []
    vectors: list[list[float]] = []
    dimension = expected_dimension
    for index, (record, embedding) in enumerate(
        zip(records, embeddings, strict=True),
        start=1,
    ):
        if not isinstance(record, Mapping) or set(record) != {
            "id",
            "document",
            "metadata",
        }:
            raise InsightConflict(f"{field} vector record {index} fields are invalid")
        record_id = _required_string(
            record["id"],
            f"{field} vector record {index} id",
        )
        document = _required_string(
            record["document"],
            f"{field} vector record {index} document",
        )
        metadata = _required_mapping(
            record["metadata"],
            f"{field} vector record {index} metadata",
        )
        normalized_metadata: dict[str, str | int | float | bool] = {}
        for key, value in metadata.items():
            if not isinstance(key, str) or not key:
                raise InsightConflict(
                    f"{field} vector record {index} metadata key is invalid"
                )
            if not isinstance(value, (str, int, float, bool)):
                raise InsightConflict(
                    f"{field} vector record {index} metadata value is invalid"
                )
            if isinstance(value, float) and not math.isfinite(value):
                raise InsightConflict(
                    f"{field} vector record {index} metadata value is invalid"
                )
            normalized_metadata[key] = value
        if (
            not isinstance(embedding, Sequence)
            or isinstance(embedding, (str, bytes, bytearray))
            or not embedding
            or any(
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not math.isfinite(float(value))
                for value in embedding
            )
        ):
            raise InsightConflict(f"{field} embedding {index} is invalid")
        if dimension is None:
            dimension = len(embedding)
        elif len(embedding) != dimension:
            raise InsightConflict(f"{field} embedding dimensions do not match")
        ids.append(record_id)
        documents.append(document)
        metadatas.append(normalized_metadata)
        vectors.append([float(value) for value in embedding])
    if len(set(ids)) != len(ids):
        raise InsightConflict(f"{field} vector record ids are duplicated")
    if dimension is None:
        raise InsightConflict(f"{field} embedding dimension is missing")
    return ids, documents, metadatas, vectors, dimension


@dataclass(frozen=True, slots=True)
class VectorCollectionInspection:
    expected: tuple[str, ...]
    actual: tuple[str, ...]
    missing: tuple[str, ...]
    orphaned: tuple[str, ...]


class InsightVectorStore:
    """Generation-isolated Chroma collections owned exclusively by Worker."""

    def __init__(self, data_root: Path) -> None:
        self.path = data_root / "chroma"

    @staticmethod
    def names(book_id: str, generation: int) -> tuple[str, str]:
        prefix = hashlib.sha256(book_id.encode("utf-8")).hexdigest()[:20]
        return (
            f"b{prefix}_g{generation}_pages",
            f"b{prefix}_g{generation}_events",
        )

    def publish(
        self,
        *,
        book_id: str,
        generation: int,
        page_records: Sequence[Mapping[str, Any]],
        page_embeddings: Sequence[Sequence[float]],
        event_records: Sequence[Mapping[str, Any]],
        event_embeddings: Sequence[Sequence[float]],
    ) -> None:
        self.publish_batches(
            book_id=book_id,
            generation=generation,
            page_batches=((page_records, page_embeddings),) if page_records else (),
            event_batches=((event_records, event_embeddings),) if event_records else (),
            expected_page_count=len(page_records),
            expected_event_count=len(event_records),
        )

    def publish_batches(
        self,
        *,
        book_id: str,
        generation: int,
        page_batches: Iterable[
            tuple[Sequence[Mapping[str, Any]], Sequence[Sequence[float]]]
        ],
        event_batches: Iterable[
            tuple[Sequence[Mapping[str, Any]], Sequence[Sequence[float]]]
        ],
        resume: bool = False,
        initial_page_count: int = 0,
        initial_event_count: int = 0,
        expected_page_count: int | None = None,
        expected_event_count: int | None = None,
        on_batch: Callable[[str, int], None] | None = None,
    ) -> dict[str, int]:
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError as exc:
            raise InsightPrecondition("ChromaDB is not installed") from exc
        self.path.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(
            path=str(self.path),
            settings=Settings(anonymized_telemetry=False),
        )
        page_name, event_name = self.names(book_id, generation)
        if resume:
            pages_collection = client.get_or_create_collection(
                page_name,
                metadata={"hnsw:space": "cosine"},
            )
            events_collection = client.get_or_create_collection(
                event_name,
                metadata={"hnsw:space": "cosine"},
            )
        else:
            existing = {
                _required_string(
                    getattr(collection, "name", collection),
                    "Chroma collection name",
                )
                for collection in client.list_collections()
            }
            for name in (page_name, event_name):
                if name in existing:
                    client.delete_collection(name)
            pages_collection = client.create_collection(
                page_name,
                metadata={"hnsw:space": "cosine"},
            )
            events_collection = client.create_collection(
                event_name,
                metadata={"hnsw:space": "cosine"},
            )
        page_count = initial_page_count
        event_count = initial_event_count
        page_dimension: int | None = None
        event_dimension: int | None = None
        try:
            for page_records, page_embeddings in page_batches:
                (
                    page_ids,
                    page_documents,
                    page_metadatas,
                    page_vectors,
                    page_dimension,
                ) = _validated_vector_batch(
                    page_records,
                    page_embeddings,
                    field="page",
                    expected_dimension=page_dimension,
                )
                pages_collection.upsert(
                    ids=page_ids,
                    embeddings=page_vectors,
                    documents=page_documents,
                    metadatas=page_metadatas,
                )
                page_count += len(page_records)
                if on_batch is not None:
                    on_batch("pages", page_count)
            for event_records, event_embeddings in event_batches:
                (
                    event_ids,
                    event_documents,
                    event_metadatas,
                    event_vectors,
                    event_dimension,
                ) = _validated_vector_batch(
                    event_records,
                    event_embeddings,
                    field="event",
                    expected_dimension=event_dimension,
                )
                events_collection.upsert(
                    ids=event_ids,
                    embeddings=event_vectors,
                    documents=event_documents,
                    metadatas=event_metadatas,
                )
                event_count += len(event_records)
                if on_batch is not None:
                    on_batch("events", event_count)
            if (
                expected_page_count is not None
                and pages_collection.count() != expected_page_count
            ):
                raise InsightConflict("page vector coverage is incomplete")
            if (
                expected_event_count is not None
                and events_collection.count() != expected_event_count
            ):
                raise InsightConflict("event vector coverage is incomplete")
            return {
                "pageCount": page_count,
                "eventCount": event_count,
            }
        except AttemptFenced:
            # A newer attempt owns publication. Keep the last fenced checkpoint;
            # the replacement attempt will idempotently upsert from that offset.
            raise
        except Exception:
            for name in (page_name, event_name):
                try:
                    client.delete_collection(name)
                except Exception:
                    # 清理旧向量集合属于尽力而为：删不掉会留下垃圾集合，
                    # 但不该掩盖正在往上抛的原始失败，所以保留异常并记日志。
                    LOGGER.warning("向量集合清理失败，可能遗留旧集合：%s", name, exc_info=True)
            raise

    def expected_collection_names(self, engine: Engine) -> set[str]:
        expected: set[str] = set()
        with engine.connect() as connection:
            rows = connection.execute(
                select(
                    vector_generations.c.book_id,
                    vector_generations.c.generation,
                ).where(vector_generations.c.status != "failed")
            )
            for book_id, generation in rows:
                expected.update(
                    self.names(
                        _required_string(book_id, "vector generation bookId"),
                        _required_integer(
                            generation,
                            "vector generation number",
                            minimum=1,
                        ),
                    )
                )
        return expected

    def inspect_collections(self, engine: Engine) -> VectorCollectionInspection:
        expected = self.expected_collection_names(engine)
        if not self.path.exists() or not any(self.path.iterdir()):
            return VectorCollectionInspection(
                expected=tuple(sorted(expected)),
                actual=(),
                missing=tuple(sorted(expected)),
                orphaned=(),
            )
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError as exc:
            raise InsightPrecondition("ChromaDB is not installed") from exc
        client = chromadb.PersistentClient(
            path=str(self.path),
            settings=Settings(anonymized_telemetry=False),
        )
        actual = {
            _required_string(
                getattr(collection, "name", collection),
                "Chroma collection name",
            )
            for collection in client.list_collections()
        }
        managed = {
            name
            for name in actual
            if re.fullmatch(r"b[0-9a-f]{20}_g[1-9][0-9]*_(?:pages|events)", name)
        }
        return VectorCollectionInspection(
            expected=tuple(sorted(expected)),
            actual=tuple(sorted(actual)),
            missing=tuple(sorted(expected - actual)),
            orphaned=tuple(sorted(managed - expected)),
        )

    def collect_orphan_collections(self, engine: Engine) -> int:
        inspection = self.inspect_collections(engine)
        if not inspection.orphaned:
            return 0
        import chromadb
        from chromadb.config import Settings

        client = chromadb.PersistentClient(
            path=str(self.path),
            settings=Settings(anonymized_telemetry=False),
        )
        deleted = 0
        for name in inspection.orphaned:
            client.delete_collection(name)
            deleted += 1
        return deleted


class InsightDerivedRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _assert_book_owned(connection: Connection, book_id: str) -> None:
        if connection.execute(
            select(books.c.id).where(
                books.c.id == book_id,
                books.c.kind == "library",
                books.c.owner_user_id == effective_owner_id(),
            )
        ).scalar_one_or_none() is None:
            raise InsightNotFound("book not found")

    def snapshot(
        self,
        *,
        book_id: str,
        frozen_inputs: Sequence[Mapping[str, Any]] | None = None,
    ) -> AnalysisInputSnapshot:
        with self.engine.connect() as connection:
            self._assert_book_owned(connection, book_id)
            return self._snapshot(
                connection,
                book_id=book_id,
                frozen_inputs=frozen_inputs,
            )

    def snapshot_in_transaction(
        self,
        connection: Connection,
        *,
        book_id: str,
        frozen_inputs: Sequence[Mapping[str, Any]] | None = None,
    ) -> AnalysisInputSnapshot:
        """Read the current analysis snapshot inside a caller-owned transaction."""

        self._assert_book_owned(connection, book_id)
        return self._snapshot(
            connection,
            book_id=book_id,
            frozen_inputs=frozen_inputs,
        )

    def snapshot_for_run(self, *, run_id: str) -> AnalysisInputSnapshot:
        """Read successful new and reused pages for whole-book post-processing."""

        source_pointer = page_assets.alias("run_snapshot_source")
        with self.engine.connect() as connection:
            run = connection.execute(
                select(
                    analysis_runs.c.book_id,
                    analysis_runs.c.status,
                ).where(
                    analysis_runs.c.id == run_id,
                    analysis_runs.c.owner_user_id == effective_owner_id(),
                )
            ).mappings().one_or_none()
            if run is None:
                raise InsightNotFound("analysis run not found")
            rows = list(
                connection.execute(
                    select(
                        analysis_page_results,
                        analysis_run_targets.c.chapter_id,
                        analysis_run_targets.c.ordinal.label("target_ordinal"),
                        source_pointer.c.asset_id.label(
                            "current_source_asset_id"
                        ),
                        assets.c.checksum.label("current_source_checksum"),
                    )
                    .join(
                        analysis_run_targets,
                        (analysis_run_targets.c.run_id == analysis_page_results.c.run_id)
                        & (
                            analysis_run_targets.c.page_id_snapshot
                            == analysis_page_results.c.page_id_snapshot
                        ),
                    )
                    .join(
                        source_pointer,
                        (source_pointer.c.page_id == analysis_run_targets.c.page_id)
                        & (source_pointer.c.role == "source"),
                    )
                    .join(assets, assets.c.id == source_pointer.c.asset_id)
                    .where(
                        analysis_page_results.c.run_id == run_id,
                        analysis_run_targets.c.status == "completed",
                    )
                    .order_by(analysis_run_targets.c.ordinal)
                ).mappings()
            )
        if not rows:
            raise InsightConflict("analysis run has no successful page results")
        run_status = _required_string(run["status"], "analysis run status")
        if run_status != "staging":
            raise InsightConflict("analysis run is not staging")
        extra_entities = get_run_extra_entities(run_id, self.engine)
        page_payloads: list[dict[str, Any]] = []
        seen_page_ids: set[str] = set()
        previous_ordinal = 0
        for index, row in enumerate(rows, start=1):
            target_ordinal = _required_integer(
                row["target_ordinal"],
                f"analysis target {index} ordinal",
                minimum=1,
            )
            if target_ordinal <= previous_ordinal:
                raise InsightInvariant(
                    "stored analysis target order is invalid"
                )
            previous_ordinal = target_ordinal
            if _required_string(
                row["status"],
                "analysis page result status",
            ) not in {"staging", "published"}:
                raise InsightInvariant(
                    "stored staging page analysis is invalid"
                )
            page_id = _required_string(
                row["page_id_snapshot"],
                "analysis page result pageId",
            )
            if page_id in seen_page_ids:
                raise InsightInvariant(
                    "stored analysis page results are duplicated"
                )
            seen_page_ids.add(page_id)
            if _required_string(
                row["page_id"],
                "analysis page result current pageId",
            ) != page_id:
                raise InsightInvariant(
                    "stored analysis page identity is invalid"
                )
            page_number = _required_integer(
                row["page_number_snapshot"],
                "analysis page result pageNumber",
                minimum=1,
            )
            source_asset_id = _required_string(
                row["source_asset_id"],
                "analysis page result source asset id",
            )
            source_checksum = _required_sha256(
                row["source_checksum"],
                "analysis page result source checksum",
            )
            if (
                _required_string(
                    row["current_source_asset_id"],
                    "analysis page current source asset id",
                )
                != source_asset_id
                or _required_sha256(
                    row["current_source_checksum"],
                    "analysis page current source checksum",
                )
                != source_checksum
            ):
                raise InsightConflict(
                    "analysis page source changed before derived processing"
                )
            page_payloads.append(
                {
                    "resultId": _required_string(
                        row["id"],
                        "analysis page result id",
                    ),
                    "pageId": page_id,
                    "pageNumber": page_number,
                    "chapterId": _optional_string(
                        row["chapter_id"],
                        "analysis page result chapterId",
                    ),
                    "sourceChecksum": source_checksum,
                    "currentSourceChecksum": source_checksum,
                    "analysis": _persisted_page_analysis(
                        row["payload_json"],
                        page_id=page_id,
                        page_number=page_number,
                        source_asset_id=source_asset_id,
                        source_checksum=source_checksum,
                        run_id=run_id,
                        connection=self.engine,
                        extra_entities=extra_entities,
                    ),
                }
            )
        pages_payload = tuple(page_payloads)
        fingerprint = _analysis_input_fingerprint(pages_payload)
        return AnalysisInputSnapshot(
            book_id=_required_string(run["book_id"], "analysis run bookId"),
            source_run_id=run_id,
            source_run_status=run_status,
            result_ids=tuple(
                _required_string(row["id"], "analysis page result id")
                for row in rows
            ),
            pages=pages_payload,
            fingerprint=fingerprint,
        )

    @staticmethod
    def _snapshot(
        connection: Connection,
        *,
        book_id: str,
        frozen_inputs: Sequence[Mapping[str, Any]] | None = None,
    ) -> AnalysisInputSnapshot:
        book_head = connection.execute(
                select(
                    analysis_heads.c.active_run_id,
                    analysis_heads.c.updated_at.label("head_updated_at"),
                    analysis_runs.c.status,
                )
                .join(
                    analysis_runs,
                    analysis_runs.c.id == analysis_heads.c.active_run_id,
                )
                .where(
                    analysis_heads.c.book_id == book_id,
                    analysis_heads.c.page_id.is_(None),
                )
            ).mappings().one_or_none()
        source_pointer = page_assets.alias("derived_current_source")
        numbered_pages = (
            select(
                pages.c.id.label("page_id"),
                pages.c.chapter_id,
                chapters.c.ordinal.label("chapter_ordinal"),
                pages.c.ordinal.label("page_ordinal"),
                func.row_number()
                .over(order_by=(chapters.c.ordinal, pages.c.ordinal))
                .label("current_page_number"),
            )
            .join(chapters, chapters.c.id == pages.c.chapter_id)
            .where(chapters.c.book_id == book_id)
            .subquery("derived_numbered_pages")
        )
        if frozen_inputs is None:
            active_run_id: str | None = None
            active_head_updated_at: datetime | None = None
            active_target_statuses: dict[str, str] = {}
            if book_head is not None:
                active_run_id = _required_string(
                    book_head["active_run_id"],
                    "active Insight run id",
                )
                active_head_updated_at = _required_datetime(
                    book_head["head_updated_at"],
                    "active Insight book head updatedAt",
                )
                active_run_status = _required_string(
                    book_head["status"],
                    "active Insight run status",
                )
                if active_run_status not in FINAL_ANALYSIS_RUN_STATUSES:
                    raise InsightConflict(
                        "active Insight run status is invalid"
                    )
                for target in connection.execute(
                    select(
                        analysis_run_targets.c.page_id_snapshot,
                        analysis_run_targets.c.status,
                    ).where(
                        analysis_run_targets.c.run_id == active_run_id
                    )
                ).mappings():
                    target_page_id = _required_string(
                        target["page_id_snapshot"],
                        "active Insight target page id",
                    )
                    target_status = _required_string(
                        target["status"],
                        "active Insight target status",
                    )
                    if (
                        target_page_id in active_target_statuses
                        or target_status
                        not in {"completed", "failed", "conflict"}
                    ):
                        raise InsightConflict(
                            "active Insight run targets are invalid"
                        )
                    active_target_statuses[target_page_id] = target_status
            rows = list(
                connection.execute(
                    select(
                        analysis_page_results,
                        numbered_pages.c.chapter_ordinal,
                        numbered_pages.c.page_ordinal,
                        numbered_pages.c.current_page_number,
                        analysis_heads.c.updated_at.label(
                            "page_head_updated_at"
                        ),
                        source_pointer.c.asset_id.label(
                            "current_source_asset_id"
                        ),
                        assets.c.checksum.label("current_source_checksum"),
                    )
                    .join(
                        analysis_heads,
                        analysis_heads.c.active_result_id
                        == analysis_page_results.c.id,
                    )
                    .join(
                        numbered_pages,
                        numbered_pages.c.page_id == analysis_heads.c.page_id,
                    )
                    .join(
                        source_pointer,
                        (source_pointer.c.page_id == numbered_pages.c.page_id)
                        & (source_pointer.c.role == "source"),
                    )
                    .join(assets, assets.c.id == source_pointer.c.asset_id)
                    .where(
                        analysis_heads.c.book_id == book_id,
                        analysis_page_results.c.source_checksum == assets.c.checksum,
                    )
                    .order_by(
                        numbered_pages.c.chapter_ordinal,
                        numbered_pages.c.page_ordinal,
                    )
                ).mappings()
            )
            if active_run_id is not None:
                if active_head_updated_at is None:
                    raise InsightConflict(
                        "active Insight book head timestamp is missing"
                    )
                current_rows: list[Mapping[str, Any]] = []
                for row in rows:
                    row_page_id = _required_string(
                        row["page_id_snapshot"],
                        "current analysis pageId",
                    )
                    row_run_id = _required_string(
                        row["run_id"],
                        "current analysis run id",
                    )
                    is_fallback = (
                        row_run_id != active_run_id
                        and active_target_statuses.get(row_page_id)
                        in {"failed", "conflict"}
                        and _required_datetime(
                            row["page_head_updated_at"],
                            "current analysis page head updatedAt",
                        )
                        <= active_head_updated_at
                    )
                    if not is_fallback:
                        current_rows.append(row)
                rows = current_rows
            if not rows and book_head is None:
                raise InsightNotFound("book has no published page analysis")
            ordered_inputs = []
            for row in rows:
                page_id = _required_string(
                    row["page_id_snapshot"],
                    "current analysis pageId",
                )
                _required_integer(
                    row["page_number_snapshot"],
                    "analysis page number snapshot",
                    minimum=1,
                )
                current_page_number = _required_integer(
                    row["current_page_number"],
                    "current page number",
                    minimum=1,
                )
                source_asset_id = _required_string(
                    row["source_asset_id"],
                    "current analysis source asset id",
                )
                source_checksum = _required_sha256(
                    row["source_checksum"],
                    "current analysis source checksum",
                )
                if (
                    _required_string(
                        row["page_id"],
                        "current analysis page id",
                    )
                    != page_id
                    or _required_string(
                        row["current_source_asset_id"],
                        "current page source asset id",
                    )
                    != source_asset_id
                    or _required_sha256(
                        row["current_source_checksum"],
                        "current page source checksum",
                    )
                    != source_checksum
                ):
                    raise InsightPrecondition(
                        "published page analysis is stale; reanalyze the page"
                    )
                ordered_inputs.append(
                    {
                        "resultId": _required_string(
                            row["id"],
                            "current analysis result id",
                        ),
                        "pageId": page_id,
                        "pageNumber": current_page_number,
                        "currentSourceChecksum": source_checksum,
                    }
                )
        else:
            ordered_inputs = []
            for index, value in enumerate(frozen_inputs):
                if not isinstance(value, Mapping):
                    raise InsightConflict(
                        "frozen analysis input must be an object"
                    )
                item = dict(value)
                if set(item) != {
                    "resultId",
                    "pageId",
                    "pageNumber",
                    "currentSourceChecksum",
                }:
                    raise InsightConflict("frozen analysis input fields are invalid")
                ordered_inputs.append(
                    {
                        "resultId": _required_string(
                            item["resultId"],
                            f"frozen analysis input {index + 1} resultId",
                        ),
                        "pageId": _required_string(
                            item["pageId"],
                            f"frozen analysis input {index + 1} pageId",
                        ),
                        "pageNumber": _required_integer(
                            item["pageNumber"],
                            f"frozen analysis input {index + 1} pageNumber",
                            minimum=1,
                        ),
                        "currentSourceChecksum": _required_sha256(
                            item["currentSourceChecksum"],
                            f"frozen analysis input {index + 1} currentSourceChecksum",
                        ),
                    }
                )
            result_ids = [value["resultId"] for value in ordered_inputs]
            if not result_ids or len(set(result_ids)) != len(result_ids):
                raise InsightConflict("frozen analysis inputs are invalid")
            frozen_page_ids = [value["pageId"] for value in ordered_inputs]
            if len(set(frozen_page_ids)) != len(frozen_page_ids):
                raise InsightConflict("frozen analysis inputs are invalid")
            current_rows = list(
                connection.execute(
                    select(
                        numbered_pages.c.page_id,
                        numbered_pages.c.current_page_number,
                        assets.c.checksum.label("current_source_checksum"),
                    )
                    .join(
                        source_pointer,
                        (source_pointer.c.page_id == numbered_pages.c.page_id)
                        & (source_pointer.c.role == "source"),
                    )
                    .join(assets, assets.c.id == source_pointer.c.asset_id)
                    .where(numbered_pages.c.page_id.in_(frozen_page_ids))
                ).mappings()
            )
            current_by_page: dict[str, Mapping[str, Any]] = {}
            for row in current_rows:
                current_page_id = _required_string(
                    row["page_id"],
                    "current frozen analysis page id",
                )
                if current_page_id in current_by_page:
                    raise InsightConflict(
                        "current frozen analysis pages are duplicated"
                    )
                current_by_page[current_page_id] = row
            if set(current_by_page) != set(frozen_page_ids):
                raise InsightConflict(
                    "frozen analysis pages no longer match current content"
                )
            for frozen_input in ordered_inputs:
                current = current_by_page[frozen_input["pageId"]]
                if (
                    _required_integer(
                        current["current_page_number"],
                        "current frozen analysis page number",
                        minimum=1,
                    )
                    != frozen_input["pageNumber"]
                    or _required_sha256(
                        current["current_source_checksum"],
                        "current frozen analysis source checksum",
                    )
                    != frozen_input["currentSourceChecksum"]
                ):
                    raise InsightConflict(
                        "frozen analysis inputs no longer match current content"
                    )
            selected = list(
                connection.execute(
                    select(analysis_page_results).where(
                        analysis_page_results.c.id.in_(tuple(result_ids))
                    )
                ).mappings()
            )
            by_id: dict[str, Mapping[str, Any]] = {}
            for row in selected:
                result_id = _required_string(
                    row["id"],
                    "frozen analysis result id",
                )
                if result_id in by_id:
                    raise InsightConflict(
                        "frozen analysis results are duplicated"
                    )
                by_id[result_id] = row
            if set(by_id) != set(result_ids):
                raise InsightNotFound("frozen analysis input no longer exists")
            rows = [by_id[value] for value in result_ids]
            for row, frozen_input in zip(rows, ordered_inputs):
                if _required_string(
                    row["page_id_snapshot"],
                    "frozen analysis result pageId",
                ) != frozen_input["pageId"]:
                    raise InsightConflict(
                        "frozen analysis input does not match its result"
                    )
        if not rows:
            raise InsightNotFound("book has no published page analysis")
        if len(rows) != len(ordered_inputs):
            raise InsightConflict("analysis input rows are incomplete")
        page_payloads: list[dict[str, Any]] = []
        seen_result_ids: set[str] = set()
        seen_page_ids: set[str] = set()
        seen_page_numbers: set[int] = set()
        for row, frozen_input in zip(rows, ordered_inputs):
            result_id = _required_string(
                row["id"],
                "analysis page result id",
            )
            page_id = frozen_input["pageId"]
            page_number = frozen_input["pageNumber"]
            source_asset_id = _required_string(
                row["source_asset_id"],
                "analysis page source asset id",
            )
            source_checksum = _required_sha256(
                row["source_checksum"],
                "analysis page source checksum",
            )
            analysis_page_number = _required_integer(
                row["page_number_snapshot"],
                "analysis page snapshot page number",
                minimum=1,
            )
            if (
                result_id in seen_result_ids
                or page_id in seen_page_ids
                or page_number in seen_page_numbers
            ):
                raise InsightConflict(
                    "analysis inputs are duplicated"
                )
            seen_result_ids.add(result_id)
            seen_page_ids.add(page_id)
            seen_page_numbers.add(page_number)
            if (
                _required_string(
                    row["status"],
                    "analysis page result status",
                )
                != "published"
                or _required_string(
                    row["page_id"],
                    "analysis page current page id",
                )
                != page_id
                or _required_string(
                    row["page_id_snapshot"],
                    "analysis page snapshot page id",
                )
                != page_id
                or source_checksum != frozen_input["currentSourceChecksum"]
            ):
                raise InsightConflict(
                    "published page analysis identity is invalid"
                )
            page_payloads.append(
                {
                    "resultId": result_id,
                    "pageId": page_id,
                    "pageNumber": page_number,
                    "sourceChecksum": source_checksum,
                    "currentSourceChecksum": frozen_input[
                        "currentSourceChecksum"
                    ],
                    "analysis": _persisted_page_analysis(
                        row["payload_json"],
                        page_id=page_id,
                        page_number=analysis_page_number,
                        source_asset_id=source_asset_id,
                        source_checksum=source_checksum,
                        run_id=row["run_id"],
                        connection=connection,
                    ),
                }
            )
        pages_payload = tuple(page_payloads)
        fingerprint = _analysis_input_fingerprint(pages_payload)
        source_run_id: str | None = None
        source_run_status: str | None = None
        if book_head is not None:
            active_run_id = _required_string(
                book_head["active_run_id"],
                "active Insight run id",
            )
            active_run_status = _required_string(
                book_head["status"],
                "active Insight run status",
            )
            if active_run_status not in FINAL_ANALYSIS_RUN_STATUSES:
                raise InsightConflict(
                    "active Insight run status is invalid"
                )
            selected_run_ids = {
                _required_string(row["run_id"], "analysis page result run id")
                for row in rows
            }
            if selected_run_ids == {active_run_id}:
                source_run_id = active_run_id
                source_run_status = active_run_status
                # 状态只按本次 run 的目标页判定：run 的 status 列已由 finalize_run 依据
                # 该 run 自身的 failed_count / postprocess 结果写明（repository.finalize_run），
                # 「只覆盖全书一部分」不再折算成 completed_with_errors —— 覆盖范围由产物
                # 自带的 coverage 元数据表达（发现 #28）。
        return AnalysisInputSnapshot(
            book_id=book_id,
            source_run_id=source_run_id,
            source_run_status=source_run_status,
            result_ids=tuple(
                _required_string(row["id"], "analysis page result id")
                for row in rows
            ),
            pages=pages_payload,
            fingerprint=fingerprint,
        )

    def publish_artifact(
        self,
        *,
        connection: Connection,
        frozen: AnalysisInputSnapshot,
        kind: str,
        template: str,
        payload: Mapping[str, Any],
        activate: bool = True,
    ) -> dict[str, Any]:
        canonical_payload = validate_artifact_payload(
            kind=kind,
            template=template,
            payload=payload,
        )
        status = "building"
        should_activate = False
        if activate:
            current = self._snapshot(connection, book_id=frozen.book_id)
            status = _publication_status(frozen, current)
            should_activate = status in {"ready", "degraded"}
        now = utcnow()
        revision = _required_integer(
            connection.execute(
                select(
                    func.coalesce(
                        func.max(analysis_artifacts.c.revision),
                        0,
                    )
                    + 1
                ).where(
                    analysis_artifacts.c.book_id == frozen.book_id,
                    analysis_artifacts.c.kind == kind,
                    analysis_artifacts.c.template == template,
                )
            ).scalar_one(),
            "Insight artifact revision",
            minimum=1,
        )
        if should_activate:
            connection.execute(
                update(analysis_artifacts)
                .where(
                    analysis_artifacts.c.book_id == frozen.book_id,
                    analysis_artifacts.c.kind == kind,
                    analysis_artifacts.c.template == template,
                    analysis_artifacts.c.is_active.is_(True),
                )
                .values(is_active=False, updated_at=now)
            )
        artifact_id = str(uuid.uuid4())
        connection.execute(
            insert(analysis_artifacts).values(
                id=artifact_id,
                book_id=frozen.book_id,
                run_id=frozen.source_run_id,
                kind=kind,
                template=template,
                status=status,
                revision=revision,
                is_active=should_activate,
                dependency_fingerprint=frozen.fingerprint,
                payload_json=_json(canonical_payload),
                asset_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        return {
            "artifactId": artifact_id,
            "kind": kind,
            "template": template,
            "status": status,
            "revision": revision,
            "dependencyFingerprint": frozen.fingerprint,
        }

    def layer_units(
        self,
        *,
        run_id: str,
        layer_index: int,
        config: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        analysis_config = _required_mapping(
            config.get("analysis"),
            "frozen Insight analysis config",
        )
        raw_layers = analysis_config.get("layers")
        if (
            not isinstance(raw_layers, list)
            or layer_index < 0
            or layer_index >= len(raw_layers)
            or not isinstance(raw_layers[layer_index], Mapping)
        ):
            raise InsightConflict("frozen Insight layer definition is invalid")
        layer = dict(raw_layers[layer_index])
        if (
            layer.get("index") != layer_index
            or not isinstance(layer.get("name"), str)
            or not layer["name"].strip()
        ):
            raise InsightConflict("frozen Insight layer identity is invalid")
        units_per_group = _required_integer(
            layer.get("unitsPerGroup"),
            "frozen Insight layer unitsPerGroup",
        )
        align_to_chapter = layer.get("alignToChapter")
        if not isinstance(align_to_chapter, bool):
            raise InsightConflict(
                "frozen Insight layer alignToChapter must be boolean"
            )
        preserve_chapter_boundaries = any(
            isinstance(value, Mapping)
            and value.get("alignToChapter") is True
            for value in raw_layers[layer_index:]
        )
        if layer_index == 0:
            frozen = self.snapshot_for_run(run_id=run_id)
            source_units = [
                {
                    "content": _required_mapping(
                        page.get("analysis"),
                        "frozen Insight page analysis",
                    ),
                    "pages": [
                        {
                            "pageId": _required_string(
                                page.get("pageId"),
                                "frozen Insight pageId",
                            ),
                            "pageNumber": _required_integer(
                                page.get("pageNumber"),
                                "frozen Insight pageNumber",
                                minimum=1,
                            ),
                            "chapterId": page.get("chapterId"),
                        }
                    ],
                }
                for page in frozen.pages
            ]
            default_group_size = _required_integer(
                analysis_config.get("pagesPerBatch"),
                "frozen Insight pagesPerBatch",
                minimum=1,
            )
            # 自定义架构下第 1 层的 unitsPerGroup 表示「每组合并多少页」，用于把多个
            # 页批次合并成一次聚合；内置架构沿用每批页数，保持历史行为不变。
            custom_group_size = (
                units_per_group
                if analysis_config.get("architecturePreset") == "custom"
                and units_per_group >= 1
                else 0
            )
            group_size = custom_group_size or default_group_size
        else:
            with self.engine.connect() as connection:
                rows = list(
                    connection.execute(
                        select(analysis_layer_results)
                        .where(
                            analysis_layer_results.c.run_id == run_id,
                            analysis_layer_results.c.layer_index
                            == layer_index - 1,
                            analysis_layer_results.c.status == "staging",
                        )
                        .order_by(analysis_layer_results.c.unit_index)
                    ).mappings()
                )
                covered_by_result: dict[str, list[Mapping[str, Any]]] = {}
                if rows:
                    for page in connection.execute(
                        select(analysis_layer_result_pages)
                        .where(
                            analysis_layer_result_pages.c.layer_result_id.in_(
                                tuple(
                                    _required_string(
                                        row["id"],
                                        "analysis layer result id",
                                    )
                                    for row in rows
                                )
                            )
                        )
                        .order_by(
                            analysis_layer_result_pages.c.layer_result_id,
                            analysis_layer_result_pages.c.ordinal,
                        )
                    ).mappings():
                        covered_by_result.setdefault(
                            _required_string(
                                page["layer_result_id"],
                                "analysis layer result id",
                            ),
                            [],
                        ).append(page)
                source_units = []
                for row in rows:
                    row_id = _required_string(
                        row["id"],
                        "analysis layer result id",
                    )
                    covered = covered_by_result.get(row_id, ())
                    source_units.append(
                        {
                            "content": _json_object(
                                row["content_json"],
                                "analysis layer content",
                            ),
                            "pages": [
                                {
                                    "pageId": _required_string(
                                        page["page_id_snapshot"],
                                        "analysis layer pageId",
                                    ),
                                    "pageNumber": _required_integer(
                                        page["page_number_snapshot"],
                                        "analysis layer pageNumber",
                                        minimum=1,
                                    ),
                                    "chapterId": _optional_string(
                                        row["chapter_id"],
                                        "analysis layer chapterId",
                                    ),
                                }
                                for page in covered
                            ],
                        }
                    )
            if not source_units:
                raise InsightConflict(
                    f"Insight layer {layer_index - 1} has no staging units"
                )
            group_size = units_per_group or len(source_units)

        grouped: list[list[dict[str, Any]]] = []
        if preserve_chapter_boundaries:
            by_chapter: dict[str, list[dict[str, Any]]] = {}
            chapter_order: list[str] = []
            for source_index, unit in enumerate(source_units):
                chapters_in_unit = {
                    page.get("chapterId")
                    for page in unit["pages"]
                    if page.get("chapterId") is not None
                }
                if (
                    len(chapters_in_unit) != 1
                    or not all(
                        isinstance(chapter_id, str) and chapter_id
                        for chapter_id in chapters_in_unit
                    )
                ):
                    raise InsightConflict(
                        f"Insight source unit {source_index + 1} crosses chapter boundaries"
                    )
                chapter_key = next(iter(chapters_in_unit))
                if chapter_key not in by_chapter:
                    chapter_order.append(chapter_key)
                    by_chapter[chapter_key] = []
                by_chapter[chapter_key].append(unit)
            for chapter_key in chapter_order:
                chapter_units = by_chapter[chapter_key]
                size = group_size or len(chapter_units)
                grouped.extend(
                    chapter_units[offset : offset + size]
                    for offset in range(0, len(chapter_units), size)
                )
        else:
            size = group_size or len(source_units)
            grouped = [
                source_units[offset : offset + size]
                for offset in range(0, len(source_units), size)
            ]
        result: list[dict[str, Any]] = []
        for unit_index, group in enumerate(grouped):
            covered_pages: list[dict[str, Any]] = []
            seen: set[str] = set()
            for source in group:
                for page in source["pages"]:
                    page_id = _required_string(
                        page.get("pageId"),
                        "Insight source pageId",
                    )
                    if page_id not in seen:
                        seen.add(page_id)
                        covered_pages.append(dict(page))
            chapters_in_group = {
                page.get("chapterId")
                for page in covered_pages
                if page.get("chapterId") is not None
            }
            chapter_id = None
            if len(chapters_in_group) == 1:
                chapter_id = _required_string(
                    next(iter(chapters_in_group)),
                    "Insight source chapterId",
                )
            result.append(
                {
                    "unitIndex": unit_index,
                    "chapterId": chapter_id,
                    "pages": covered_pages,
                    "inputs": [dict(source["content"]) for source in group],
                    "layer": {
                        **layer,
                        "promptType": _layer_prompt_type(
                            layer_index=layer_index,
                            layer_count=len(raw_layers),
                            align_to_chapter=align_to_chapter,
                        ),
                    },
                }
            )
        return result

    def summary_inputs(
        self,
        frozen: AnalysisInputSnapshot,
    ) -> tuple[dict[str, Any], ...]:
        """Use the highest complete summary layer, with compact pages as fallback."""

        fallback_items: list[dict[str, Any]] = []
        for index, page in enumerate(frozen.pages, start=1):
            result_id = _required_string(
                page.get("resultId"),
                f"Insight summary input {index} resultId",
            )
            page_id = _required_string(
                page.get("pageId"),
                f"Insight summary input {index} pageId",
            )
            page_number = _required_integer(
                page.get("pageNumber"),
                f"Insight summary input {index} pageNumber",
                minimum=1,
            )
            analysis = _required_mapping(
                page.get("analysis"),
                f"Insight summary input {index} analysis",
            )
            fallback_items.append(
                {
                    "resultId": result_id,
                    "pageId": page_id,
                    "pageIds": [page_id],
                    "pageNumber": page_number,
                    "pageNumbers": [page_number],
                    "analysis": {
                        key: value
                        for key, value in analysis.items()
                        if key
                        in {
                            "page_summary",
                            "key_events",
                            "continuity_notes",
                            "warnings",
                        }
                    },
                }
            )
        fallback = tuple(fallback_items)
        if not frozen.source_run_id:
            return fallback
        with self.engine.connect() as connection:
            rows = list(
                connection.execute(
                    select(analysis_layer_results)
                    .where(
                        analysis_layer_results.c.run_id
                        == frozen.source_run_id,
                        analysis_layer_results.c.status.in_(
                            ("staging", "published")
                        ),
                    )
                    .order_by(
                        analysis_layer_results.c.layer_index.desc(),
                        analysis_layer_results.c.unit_index,
                    )
                ).mappings()
            )
            covered_rows = list(
                connection.execute(
                    select(analysis_layer_result_pages).where(
                        analysis_layer_result_pages.c.layer_result_id.in_(
                            tuple(
                                _required_string(
                                    row["id"],
                                    "analysis layer result id",
                                )
                                for row in rows
                            )
                        )
                    )
                ).mappings()
            ) if rows else []
        covered_by_result: dict[str, list[Mapping[str, Any]]] = {}
        for page in covered_rows:
            covered_by_result.setdefault(
                _required_string(
                    page["layer_result_id"],
                    "analysis layer result id",
                ),
                [],
            ).append(page)
        by_layer: dict[int, list[Mapping[str, Any]]] = {}
        for row in rows:
            by_layer.setdefault(
                _required_integer(
                    row["layer_index"],
                    "analysis layer index",
                ),
                [],
            ).append(row)
        expected = {
            _required_string(page.get("pageId"), "Insight summary pageId")
            for page in frozen.pages
        }
        for layer_index in sorted(by_layer, reverse=True):
            layer_rows = by_layer[layer_index]
            covered = {
                _required_string(
                    page["page_id_snapshot"],
                    "analysis layer pageId",
                )
                for row in layer_rows
                for page in covered_by_result.get(
                    _required_string(row["id"], "analysis layer result id"),
                    (),
                )
            }
            if covered != expected:
                continue
            inputs: list[dict[str, Any]] = []
            for row in layer_rows:
                pages_for_result = sorted(
                    covered_by_result.get(
                        _required_string(
                            row["id"],
                            "analysis layer result id",
                        ),
                        (),
                    ),
                    key=lambda page: _required_integer(
                        page["ordinal"],
                        "analysis layer page ordinal",
                        minimum=1,
                    ),
                )
                page_ids = [
                    _required_string(
                        page["page_id_snapshot"],
                        "analysis layer pageId",
                    )
                    for page in pages_for_result
                ]
                page_numbers = [
                    _required_integer(
                        page["page_number_snapshot"],
                        "analysis layer pageNumber",
                        minimum=1,
                    )
                    for page in pages_for_result
                ]
                if not page_ids:
                    raise InsightPrecondition(
                        "analysis layer has no covered pages"
                    )
                inputs.append(
                    {
                        "resultId": _required_string(
                            row["id"],
                            "analysis layer result id",
                        ),
                        "pageId": page_ids[0],
                        "pageIds": page_ids,
                        "pageNumber": page_numbers[0],
                        "pageNumbers": page_numbers,
                        "analysis": _json_object(
                            row["content_json"],
                            "analysis layer content",
                        ),
                    }
                )
            return tuple(inputs)
        return fallback

    def compressed_context_input(
        self,
        frozen: AnalysisInputSnapshot,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            statement = select(analysis_artifacts).where(
                analysis_artifacts.c.book_id == frozen.book_id,
                analysis_artifacts.c.kind == "compressed_context",
                analysis_artifacts.c.template == "default",
            )
            if frozen.source_run_id:
                staged = connection.execute(
                    statement.where(
                        analysis_artifacts.c.run_id == frozen.source_run_id,
                        analysis_artifacts.c.status == "building",
                        analysis_artifacts.c.dependency_fingerprint
                        == frozen.fingerprint,
                    ).order_by(analysis_artifacts.c.revision.desc())
                ).mappings().first()
            else:
                staged = None
            row = staged or connection.execute(
                statement.where(
                    analysis_artifacts.c.is_active.is_(True),
                    analysis_artifacts.c.status.in_(("ready", "degraded")),
                    analysis_artifacts.c.dependency_fingerprint
                    == frozen.fingerprint,
                )
                .order_by(analysis_artifacts.c.revision.desc())
            ).mappings().first()
        if row is None:
            return None
        return {
            "resultId": _required_string(
                row["id"],
                "compressed context artifact id",
            ),
            "pageId": _required_string(
                frozen.pages[0].get("pageId"),
                "compressed context first pageId",
            ),
            "pageIds": [
                _required_string(
                    page.get("pageId"),
                    "compressed context pageId",
                )
                for page in frozen.pages
            ],
            "pageNumber": _required_integer(
                frozen.pages[0].get("pageNumber"),
                "compressed context first pageNumber",
                minimum=1,
            ),
            "pageNumbers": [
                _required_integer(
                    page.get("pageNumber"),
                    "compressed context pageNumber",
                    minimum=1,
                )
                for page in frozen.pages
            ],
            "analysis": {
                "compressed_context": _json_object(
                    row["payload_json"],
                    "compressed context payload",
                ),
            },
        }

    @staticmethod
    def publish_layer(
        connection: Connection,
        *,
        run_id: str,
        layer_index: int,
        layer_name: str,
        units: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if not units:
            raise InsightConflict("Insight layer has no publishable units")
        now = utcnow()
        for unit_index, unit in enumerate(units, start=1):
            raw_pages = unit.get("pages")
            if not isinstance(raw_pages, list) or not raw_pages or any(
                not isinstance(page, Mapping) for page in raw_pages
            ):
                raise InsightConflict("Insight layer covered pages are invalid")
            pages_covered = list(raw_pages)
            content = _required_mapping(
                unit.get("content"),
                "Insight layer content",
            )
            if not contains_nonempty_text(content):
                raise InsightConflict("Insight layer content must not be empty")
            validated_pages = []
            for page_index, page in enumerate(pages_covered, start=1):
                validated_pages.append(
                    {
                        **dict(page),
                        "pageId": _required_string(
                            page.get("pageId"),
                            f"Insight layer unit {unit_index} page {page_index} pageId",
                        ),
                        "pageNumber": _required_integer(
                            page.get("pageNumber"),
                            f"Insight layer unit {unit_index} page {page_index} pageNumber",
                            minimum=1,
                        ),
                    }
                )
            pages_covered = validated_pages
            fingerprint = hashlib.sha256(
                _json(
                    {
                        "pages": pages_covered,
                        "content": content,
                    }
                ).encode("utf-8")
            ).hexdigest()
            result_id = str(uuid.uuid4())
            page_numbers = [page["pageNumber"] for page in pages_covered]
            connection.execute(
                insert(analysis_layer_results).values(
                    id=result_id,
                    run_id=run_id,
                    layer_index=layer_index,
                    layer_name=layer_name,
                    unit_index=_required_integer(
                        unit.get("unitIndex"),
                        "Insight layer unitIndex",
                    ),
                    chapter_id=_optional_string(
                        unit.get("chapterId"),
                        "Insight layer chapterId",
                    ),
                    page_range_snapshot_json=_json(
                        {
                            "start": min(page_numbers),
                            "end": max(page_numbers),
                        }
                    ),
                    content_json=_json(content),
                    input_fingerprint=fingerprint,
                    status="staging",
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                insert(analysis_layer_result_pages),
                [
                    {
                        "layer_result_id": result_id,
                        "ordinal": ordinal,
                        "page_id": page["pageId"],
                        "page_id_snapshot": page["pageId"],
                        "page_number_snapshot": page["pageNumber"],
                    }
                    for ordinal, page in enumerate(
                        pages_covered,
                        start=1,
                    )
                ],
            )
        return {
            "runId": run_id,
            "layerIndex": layer_index,
            "unitCount": len(units),
        }

    def align_entities(
        self,
        *,
        book_id: str,
        chapter_id: str,
        pages_analysis: Sequence[Mapping[str, Any]],
        alias_seed_config: Mapping[str, Any] | None = None,
        entity_slug_map_config: Mapping[str, Mapping[str, str]] | None = None,
        llm_callable: Callable[[str], str] | None = None,
        llm_judge: "LLMBatchJudge | None" = None,
        judge_cache_path: Path | None = None,
        run_id: str | None = None,
        slug_mode: str | None = None,
        staged: bool = False,
        dependency_fingerprint: str | None = None,
        progress: Callable[[str, int | None, int | None], None] | None = None,
    ) -> dict[str, Any]:
        """Align entities extracted from chapter pages with existing canonical entities,

        progress(message, done, total) 为可选进度回调：用于把「语义裁决」这类长耗时
        阶段的分批进展写进任务日志（对齐过去整体藏在一个步骤里、对用户完全不可见）。

        supporting incremental updates, transactional locking, auto merge rules (frequency, substring overlap, model assisted),
        auto_merge_log auditing, and seed overrides.

        staged=True 时写入 status="building" / is_active=False 的暂存产物，交由所属
        analysis run 的 publish_run（finalize_run）统一激活；独立 job 调用时保持
        staged=False，直接停用旧 revision 并激活新 revision。

        dependency_fingerprint 为所属 run 的输入指纹：暂存产物必须与同一 run 的其他
        产物使用同一指纹（否则 finalize_run 判定 staged generations 不一致），因此
        staged 调用必须显式传入；未传时退化为产物内容哈希（仅适用于独立 job）。

        judge_cache_path 指向裁决缓存文件：同一本书重建时命中缓存的分片不再请求模型
        （审计管线 A）。缓存只存成功的裁决，且按 provider/model/api_key 指纹划隔离域。
        """
        # entity_slug_map 只接受调用方显式注入，缺失时退化为纯拼音（不再隐式读盘）
        slug_map = dict(entity_slug_map_config) if entity_slug_map_config else {}

        with self.engine.begin() as connection:
            self._assert_book_owned(connection, book_id)

            # 解析 slug_mode: 优先使用显式传参，若为空且有 run_id 则从 analysis_runs.config_json.analysis.slugMode 读取（唯一权威来源）
            effective_slug_mode = slug_mode
            if not effective_slug_mode and run_id:
                try:
                    stmt = select(analysis_runs.c.config_json).where(analysis_runs.c.id == run_id)
                    raw_cfg = connection.execute(stmt).scalar_one_or_none()
                    if raw_cfg:
                        if isinstance(raw_cfg, (bytes, bytearray)):
                            raw_cfg = raw_cfg.decode("utf-8")
                        cfg_obj = json.loads(raw_cfg) if isinstance(raw_cfg, str) else raw_cfg
                        ac = cfg_obj.get("analysis", {}) if isinstance(cfg_obj, Mapping) else {}
                        # analysis.slugMode 为唯一权威来源；根层仅做极弱兼容兜底
                        effective_slug_mode = ac.get("slugMode") or ac.get("slug_mode")
                        if not effective_slug_mode and isinstance(cfg_obj, Mapping):
                            effective_slug_mode = cfg_obj.get("slugMode") or cfg_obj.get("slug_mode")
                except Exception:  # noqa: BLE001 - 配置损坏时回退默认拼音模式
                    LOGGER.debug(
                        "读取 run 配置里的 slugMode 失败，回退拼音模式: run_id=%s",
                        run_id,
                        exc_info=True,
                    )
            if not effective_slug_mode:
                effective_slug_mode = "pinyin"
            effective_slug_mode = str(effective_slug_mode).strip().lower()
            if effective_slug_mode == "english":
                effective_slug_mode = "romanized"
            if effective_slug_mode not in {"pinyin", "romanized"}:
                effective_slug_mode = "pinyin"

            # 1. 悲观行锁/乐观版本读取现有 active 产物
            select_query = (
                select(analysis_artifacts)
                .where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind == "entity_alignment",
                    analysis_artifacts.c.template == "default",
                    analysis_artifacts.c.is_active.is_(True),
                )
            )
            try:
                select_query = select_query.with_for_update()
            except Exception:  # noqa: BLE001 - SQLite 不支持行锁，退化为无锁读取
                LOGGER.debug("当前数据库不支持 FOR UPDATE，读取退化为无锁", exc_info=True)

            existing_artifact = connection.execute(select_query).mappings().one_or_none()

            existing_canonical_entities: list[dict[str, Any]] = []
            existing_alias_map: dict[str, str] = {}
            existing_auto_merge_log: list[dict[str, Any]] = []
            current_revision = 0
            if existing_artifact is not None:
                current_revision = existing_artifact["revision"]
                try:
                    existing_payload = _json_object(existing_artifact["payload_json"], "existing entity_alignment payload")
                    existing_canonical_entities = list(existing_payload.get("canonical_entities", []))
                    existing_alias_map = dict(existing_payload.get("alias_map", {}))
                    # 兼容读取 auto_merge_log 或老版本的 pending_review_queue
                    existing_auto_merge_log = list(existing_payload.get("auto_merge_log", []))
                except Exception:  # noqa: BLE001 - 旧产物不可解析时从空集开始重建
                    LOGGER.debug(
                        "读取现有实体对齐产物失败，按首次对齐处理: book_id=%s",
                        book_id,
                        exc_info=True,
                    )

            # 若已有产物的 slug_mode 与当前 effective_slug_mode 不一致，或者处于 pinyin 模式但存在旧的 _pinyin_ 前缀实体：
            # 执行旧数据平滑升级，将旧 ID 彻底替换为新模式下的合法 ID，杜绝新旧 ID 并存
            if existing_canonical_entities:
                old_artifact_slug_mode = existing_payload.get("slug_mode") if existing_artifact else None
                should_refresh_ids = (
                    (old_artifact_slug_mode and old_artifact_slug_mode != effective_slug_mode)
                    or (effective_slug_mode == "pinyin" and any("_pinyin_" in ent.get("entity_id", "") for ent in existing_canonical_entities))
                )
                if should_refresh_ids:
                    id_replacements = {}
                    for ent in existing_canonical_entities:
                        old_id = ent.get("entity_id", "")
                        cat = ent.get("category", "")
                        canon = ent.get("canonical_name", "")
                        new_slug = to_romanized_slug(cat, canon, slug_map=slug_map, slug_mode=effective_slug_mode)
                        new_id = f"{CATEGORY_PREFIX.get(cat, cat)}_{new_slug}"
                        if new_id != old_id:
                            id_replacements[old_id] = new_id
                            ent["entity_id"] = new_id
                    if id_replacements:
                        for k, v in list(existing_alias_map.items()):
                            if v in id_replacements:
                                existing_alias_map[k] = id_replacements[v]

            # 2. 统计本章实体词频与出现页码（自适应解包 frozen.pages 包装结构）
            raw_stats = {
                "characters": defaultdict(lambda: {"count": 0, "pages": [], "types": []}),
                "items": defaultdict(lambda: {"count": 0, "pages": [], "types": []}),
                "abilities": defaultdict(lambda: {"count": 0, "pages": [], "types": []}),
                "locations": defaultdict(lambda: {"count": 0, "pages": [], "types": []}),
                "concepts": defaultdict(lambda: {"count": 0, "pages": [], "types": []}),
            }
            normalized_pages: list[Mapping[str, Any]] = []
            for p in pages_analysis:
                if isinstance(p, Mapping) and "analysis" in p and isinstance(p["analysis"], Mapping):
                    pn = p.get("pageNumber") or p["analysis"].get("page_number_snapshot") or p["analysis"].get("page_number")
                    page_item = dict(p["analysis"])
                    if "page_number" not in page_item and pn is not None:
                        page_item["page_number"] = pn
                    normalized_pages.append(page_item)
                else:
                    normalized_pages.append(p)

            for p in normalized_pages:
                pn = p.get("page_number") or p.get("page_number_snapshot", 0)
                for cat in raw_stats.keys():
                    for it in p.get(cat, []):
                        if isinstance(it, Mapping):
                            name = str(it.get("name", "")).strip()
                            if name:
                                raw_stats[cat][name]["count"] += 1
                                raw_stats[cat][name]["pages"].append(pn)
                                it_type = it.get("type")
                                if it_type and str(it_type).strip():
                                    raw_stats[cat][name]["types"].append(str(it_type).strip())

            # 2.1 词频按页去重：同一页可能因局部重跑（多个 run 的已发布结果同时被喂入）而出现多次，
            #     词频必须按「出现页数」统计，而不是按分析行数累加，否则重跑后频次会成倍虚高。
            for stats in raw_stats.values():
                for info in stats.values():
                    known_pages = [pn for pn in info["pages"] if pn not in (None, "", 0)]
                    if known_pages:
                        info["pages"] = sorted(set(known_pages))
                        info["count"] = len(info["pages"])

            # 3. 解析 alias_seed_config
            seed = dict(alias_seed_config or {})
            global_terms = seed.get("global_terms", {})
            chapter_overrides = seed.get("chapter_overrides", {}).get(chapter_id, {})

            # 建立已有实体的快速索引
            canonical_by_id: dict[str, dict[str, Any]] = {
                ent["entity_id"]: ent for ent in existing_canonical_entities
            }
            canonical_by_name: dict[tuple[str, str], str] = {
                (ent["category"], ent["canonical_name"]): ent["entity_id"]
                for ent in existing_canonical_entities
            }
            canonical_by_alias: dict[tuple[str, str], str] = {}
            for ent in existing_canonical_entities:
                for a in ent.get("aliases", []):
                    canonical_by_alias[(ent["category"], a)] = ent["entity_id"]

            updated_alias_map = dict(existing_alias_map)
            auto_merge_log = list(existing_auto_merge_log)

            def record_merge_log(raw_name: str, canonical_name: str, rule: str, confidence: str) -> None:
                auto_merge_log.append({
                    "raw_name": raw_name,
                    "canonical_name": canonical_name,
                    "rule": rule,
                    "confidence": confidence,
                    "chapter_id": chapter_id,
                    "timestamp": utcnow().isoformat(),
                })

            # 3.4 候选图组织 + 批量三态裁决（替代「逐对询问」）。
            # 旧实现：先按字符重合度收窄，再把候选对按 30 对/批送模型逐对判是/否；大体量作品
            # 仍会产生数万次仲裁请求（久留美 1507 页实测约 2.1 万候选对，估算 30+ 小时）。
            # 新实现：候选边组织成连通分量 → 按预算打包成少量请求 → 分量内一次性输出
            # SAME / DIFFERENT / UNCERTAIN。只有 SAME 触发合并；DIFFERENT 阻止传递合并；
            # UNCERTAIN、未进入图（低频概念、无修饰通用词）的实体一律保留为独立实体。
            alignment_config: AlignmentConfig = resolve_alignment_config()
            alignment_clusters = None
            alignment_candidates: set[str] | None = None
            alignment_summary: dict[str, Any] = {}
            alignment_deferred: list[dict[str, Any]] = []
            alignment_blocked: list[dict[str, Any]] = []
            alignment_stats: dict[str, Any] = {}

            combined_nodes: dict[str, EntityNode] = {}
            combined_edges: dict[str, CandidateEdge] = {}
            combined_components: list = []
            combined_orphans: list[str] = []
            combined_deferred: list[dict[str, Any]] = []

            def _page_numbers(info: Mapping[str, Any]) -> tuple[int, ...]:
                values: list[int] = []
                for raw in info.get("pages") or []:
                    try:
                        values.append(int(raw))
                    except (TypeError, ValueError):
                        continue
                return tuple(sorted(set(values)))

            for cat, raw_dict in raw_stats.items():
                category_nodes: list[EntityNode] = []
                for name, info in raw_dict.items():
                    category_nodes.append(EntityNode(
                        node_id=f"{cat}:{name}",
                        category=cat,
                        name=name,
                        distinct_pages=int(info.get("count") or 0),
                        pages=_page_numbers(info),
                    ))
                for ent in existing_canonical_entities:
                    if ent.get("category") != cat:
                        continue
                    canon_name = str(ent.get("canonical_name") or "").strip()
                    entity_id = str(ent.get("entity_id") or "").strip()
                    if not canon_name or not entity_id:
                        continue
                    category_nodes.append(EntityNode(
                        node_id=f"{cat}:{canon_name}",
                        category=cat,
                        name=canon_name,
                        distinct_pages=len(ent.get("pages") or []),
                        pages=tuple(sorted({
                            int(number) for number in (ent.get("pages") or [])
                            if isinstance(number, int) or str(number).strip().isdigit()
                        })),
                        existing_entity_id=entity_id,
                    ))
                deduped: dict[str, EntityNode] = {}
                for node in category_nodes:
                    deduped.setdefault(node.node_id, node)
                if not deduped:
                    continue
                try:
                    category_graph = build_alignment_graph(
                        category=cat,
                        nodes=list(deduped.values()),
                        candidate_filter=is_judge_candidate,
                        config=alignment_config,
                    )
                except Exception as _graph_err:  # noqa: BLE001
                    print(f"[align_entities] 候选图构建失败（{cat}）: {_graph_err}")
                    continue
                combined_nodes.update(category_graph.nodes)
                combined_edges.update(category_graph.edges)
                combined_components.extend(category_graph.components)
                combined_orphans.extend(category_graph.orphans)
                combined_deferred.extend(category_graph.deferred)

            alignment_graph = GraphBuildResult(
                nodes=combined_nodes,
                edges=combined_edges,
                components=combined_components,
                orphans=combined_orphans,
                deferred=combined_deferred,
            )
            alignment_candidates = {
                LLMBatchJudge.make_key(edge.category, edge.name_a, edge.name_b)
                for edge in combined_edges.values()
            }
            alignment_summary = summarize_graph(alignment_graph)
            print(f"[align_entities] 候选图：{json.dumps(alignment_summary, ensure_ascii=False)}")

            if llm_judge is not None and combined_edges:
                shards = plan_requests(graph=alignment_graph, config=alignment_config)
                print(
                    f"[align_entities] 批量裁决请求 {len(shards)} 个，"
                    f"覆盖候选边 {len(combined_edges)} 条"
                )
                _judge_progress = None
                if progress is not None:
                    progress("实体对齐：批量语义裁决", 0, len(shards))

                    def _judge_progress(done: int, total: int) -> None:  # noqa: E306
                        progress("实体对齐：批量语义裁决", done, total)

                decisions: dict[str, str] = {}
                judge_stats = None
                judge_cache = None
                judge_client = llm_judge.client
                if judge_cache_path is not None and judge_client is not None:
                    judge_cache = AlignmentJudgeCache(
                        client=judge_client,
                        path=Path(judge_cache_path),
                        scope=(
                            str(getattr(judge_client, "provider", "") or ""),
                            str(getattr(getattr(judge_client, "config", None), "model", "") or ""),
                            str(getattr(getattr(judge_client, "config", None), "base_url", "") or ""),
                            AlignmentJudgeCache._fingerprint(
                                str(getattr(getattr(judge_client, "config", None), "api_key", "") or "")
                            ),
                        ),
                    )
                    judge_client = judge_cache
                try:
                    decisions, _component_decisions, judge_stats = run_alignment_sync(
                        client=judge_client,
                        graph=alignment_graph,
                        shards=shards,
                        config=alignment_config,
                        progress=_judge_progress,
                    )
                except Exception as _judge_err:  # noqa: BLE001 - 裁决失败不得中断对齐
                    print(f"[align_entities] 批量裁决失败，本轮不做语义合并: {_judge_err}")
                finally:
                    if judge_cache is not None:
                        # 只有真正跑过的分片才值得落盘；中途被杀时留下的半个文件也由
                        # tmp+replace 兜住，这里只需要把内存里的新结果写出去。
                        judge_cache.close()
                        if judge_cache.hits or judge_cache.misses:
                            print(
                                "[align_entities] 裁决缓存："
                                f"命中 {judge_cache.hits}，未命中 {judge_cache.misses}"
                            )
                outcome = apply_decisions(graph=alignment_graph, decisions=decisions)
                alignment_clusters = outcome.clusters
                alignment_blocked = outcome.blocked
                alignment_stats = dict(outcome.metrics)
                if judge_stats is not None:
                    alignment_stats["judge"] = judge_stats.as_dict()
                print(f"[align_entities] 裁决结果：{json.dumps(alignment_stats, ensure_ascii=False)}")
            elif llm_judge is None:
                print("[align_entities] 语义裁决器未启用，本轮仅按规则归并")

            alignment_deferred = list(combined_deferred)

            def judge_same(judge, cat: str, name: str, other: str) -> bool:
                """语义裁决统一入口：只把候选图上明确裁为 SAME 的对判为同义。

                不再发起任何逐对 LLM 调用；未进入候选图、处于 UNCERTAIN、或本轮裁决
                失败的对一律判否——宁可少合并，也不误并。
                """
                if alignment_clusters is None:
                    return False
                if alignment_candidates is not None and \
                        LLMBatchJudge.make_key(cat, name, other) not in alignment_candidates:
                    return False
                return alignment_clusters.cluster_of(f"{cat}::{name}") == \
                    alignment_clusters.cluster_of(f"{cat}::{other}")

            # 4. 执行增量对齐与自动归并
            # 说明：历史上这里内嵌了一份作品专属的“已知聚类簇”（宿傩之指 / 高木 /
            # 杉泽第三高中各场景），只对特定作品生效，已移除。需要人工指定的跨名合并
            # 应走 global_terms / chapter_overrides 种子通道，而不是写死在代码里。
            for cat, raw_dict in raw_stats.items():
                # 处理独立实体，引入类别特定的受控自动归并规则
                # 按频次从高到低排序处理，确保高频标准名称优先成为 canonical 实体
                sorted_raw_items = sorted(raw_dict.items(), key=lambda it: it[1]["count"], reverse=True)
                for name, info in sorted_raw_items:

                    # 1) 精确命中已存在的 canonical_name 或 alias
                    target_id = canonical_by_name.get((cat, name)) or canonical_by_alias.get((cat, name))

                    # 2) 自动归并规则探测（若未直接命中现有实体）
                    merge_rule_hit = None
                    merge_confidence = "high"
                    if not target_id:
                        for existing_ent in canonical_by_id.values():
                            if existing_ent["category"] != cat:
                                continue
                            c_name = existing_ent["canonical_name"]
                            c_freq = existing_ent["total_frequency"]
                            c_aliases = existing_ent.get("aliases", [])

                            # 硬性拒绝规则前置：所有类别的确定性合并规则、Rule 2.1 自动合并
                            # 与 LLM 粗筛之前先拦一道，命中即判 False（不合并、不送 LLM）。
                            rejected_rule = hard_reject_pair(cat, name, c_name)
                            if rejected_rule:
                                continue

                            # 分类别定制归并判定：
                            if cat == "characters":
                                # 角色归并：专属规则（支持全名/简称、关系代称、称谓别名，禁止跨角色或同姓不同人误并）
                                # 专属规则内部已包含亲属/从属称谓防跨角色安全红线
                                if can_merge_character_names(name, c_name):
                                    target_id = existing_ent["entity_id"]
                                    merge_rule_hit = "character_alias_merge"
                                    merge_confidence = "high"
                                    break
                                for ex_alias in c_aliases:
                                    if can_merge_character_names(name, ex_alias):
                                        target_id = existing_ent["entity_id"]
                                        merge_rule_hit = "character_alias_merge"
                                        merge_confidence = "high"
                                        break
                                if target_id:
                                    break

                                # LLM 语义裁决（统一粗筛闸门：硬性拒绝 + 核心字重合度 + 短名保护）
                                if is_judge_candidate(cat, name, c_name):
                                    judge = llm_judge
                                    if judge_same(judge, cat, name, c_name):
                                        target_id = existing_ent["entity_id"]
                                        merge_rule_hit = "llm_semantic_judge"
                                        merge_confidence = "high"
                                        break
                                for ex_alias in c_aliases:
                                    if is_judge_candidate(cat, name, ex_alias):
                                        judge = llm_judge
                                        if judge_same(judge, cat, name, ex_alias):
                                            target_id = existing_ent["entity_id"]
                                            merge_rule_hit = "llm_semantic_judge"
                                            merge_confidence = "high"
                                            break
                                if target_id:
                                    break

                            elif cat == "abilities":
                                # 能力归并：带有式神/分支后缀的，核心名不同禁止合并（专属规则第一道防线）
                                # 若核心名不同，专属规则明确拒绝合并，不进入 LLM 判定
                                core1 = name.split("·")[-1] if "·" in name else name
                                core2 = c_name.split("·")[-1] if "·" in c_name else c_name
                                clean_core1 = re.sub(r'[\(（].*?[\)）]', '', core1).strip()
                                clean_core2 = re.sub(r'[\(（].*?[\)）]', '', core2).strip()

                                if can_merge_ability_names(name, c_name):
                                    target_id = existing_ent["entity_id"]
                                    merge_rule_hit = "ability_core_match"
                                    merge_confidence = "high"
                                    break
                                for ex_alias in c_aliases:
                                    if can_merge_ability_names(name, ex_alias):
                                        target_id = existing_ent["entity_id"]
                                        merge_rule_hit = "ability_core_match"
                                        merge_confidence = "high"
                                        break
                                if target_id:
                                    break

                                # 核心名不同时（如 '<体系>·<分支A>' 与 '<体系>·<分支B>'），绝对禁止合并，不走 LLM
                                if clean_core1 and clean_core2 and clean_core1 != clean_core2:
                                    pass
                                else:
                                    # LLM 语义裁决（统一粗筛闸门：硬性拒绝 + 核心字重合度 + 短名保护）
                                    if is_judge_candidate(cat, name, c_name):
                                        judge = llm_judge
                                        if judge_same(judge, cat, name, c_name):
                                            target_id = existing_ent["entity_id"]
                                            merge_rule_hit = "llm_semantic_judge"
                                            merge_confidence = "high"
                                            break
                                    for ex_alias in c_aliases:
                                        alias_core = ex_alias.split("·")[-1] if "·" in ex_alias else ex_alias
                                        clean_alias_core = re.sub(r'[\(（].*?[\)）]', '', alias_core).strip()
                                        if clean_core1 and clean_alias_core and clean_core1 != clean_alias_core:
                                            continue
                                        if is_judge_candidate(cat, name, ex_alias):
                                            judge = llm_judge
                                            if judge_same(judge, cat, name, ex_alias):
                                                target_id = existing_ent["entity_id"]
                                                merge_rule_hit = "llm_semantic_judge"
                                                merge_confidence = "high"
                                                break
                                if target_id:
                                    break

                            elif cat == "locations":
                                # 地点归并：包含 · 的必须比较最末级具体位置名称，禁止仅凭上级机构名重合合并（专属规则第一道防线）
                                is_loc_blocked = False
                                if "·" in name and "·" in c_name:
                                    p1, p2 = name.split("·"), c_name.split("·")
                                    o1, d1 = "·".join(p1[:-1]), p1[-1]
                                    o2, d2 = "·".join(p2[:-1]), p2[-1]
                                    if o1 != o2 or calculate_char_overlap_ratio(d1, d2) < 0.40:
                                        is_loc_blocked = True

                                if can_merge_location_names(name, c_name):
                                    target_id = existing_ent["entity_id"]
                                    merge_rule_hit = "location_detail_overlap"
                                    merge_confidence = "high"
                                    break
                                for ex_alias in c_aliases:
                                    if can_merge_location_names(name, ex_alias):
                                        target_id = existing_ent["entity_id"]
                                        merge_rule_hit = "location_detail_overlap"
                                        merge_confidence = "high"
                                        break
                                if target_id:
                                    break

                                # 若层级路径校验未通过（不同末级设施如操场与天台），绝对禁止合并，不走 LLM
                                if not is_loc_blocked:
                                    if is_judge_candidate(cat, name, c_name):
                                        judge = llm_judge
                                        if judge_same(judge, cat, name, c_name):
                                            target_id = existing_ent["entity_id"]
                                            merge_rule_hit = "llm_semantic_judge"
                                            merge_confidence = "high"
                                            break
                                    for ex_alias in c_aliases:
                                        alias_blocked = False
                                        if "·" in name and "·" in ex_alias:
                                            p1, p2 = name.split("·"), ex_alias.split("·")
                                            o1, d1 = "·".join(p1[:-1]), p1[-1]
                                            o2, d2 = "·".join(p2[:-1]), p2[-1]
                                            if o1 != o2 or calculate_char_overlap_ratio(d1, d2) < 0.40:
                                                alias_blocked = True
                                        if not alias_blocked and is_judge_candidate(cat, name, ex_alias):
                                            judge = llm_judge
                                            if judge_same(judge, cat, name, ex_alias):
                                                target_id = existing_ent["entity_id"]
                                                merge_rule_hit = "llm_semantic_judge"
                                                merge_confidence = "high"
                                                break
                                if target_id:
                                    break

                            elif cat == "items":
                                # 物品归并：确定性规则（全同/剥离括号后同） + 语义裁决（重合度 >= 40% 查 LLM 缓存/判定）
                                if can_merge_item_names(name, c_name):
                                    target_id = existing_ent["entity_id"]
                                    merge_rule_hit = "item_core_match"
                                    merge_confidence = "high"
                                    break
                                for ex_alias in c_aliases:
                                    if can_merge_item_names(name, ex_alias):
                                        target_id = existing_ent["entity_id"]
                                        merge_rule_hit = "item_core_match"
                                        merge_confidence = "high"
                                        break
                                if target_id:
                                    break

                                # LLM 语义裁决（字符重合度 >= 40% 粗筛）
                                if is_judge_candidate(cat, name, c_name):
                                    judge = llm_judge
                                    if judge_same(judge, cat, name, c_name):
                                        target_id = existing_ent["entity_id"]
                                        merge_rule_hit = "llm_semantic_judge"
                                        merge_confidence = "high"
                                        break
                                for ex_alias in c_aliases:
                                    if is_judge_candidate(cat, name, ex_alias):
                                        judge = llm_judge
                                        if judge_same(judge, cat, name, ex_alias):
                                            target_id = existing_ent["entity_id"]
                                            merge_rule_hit = "llm_semantic_judge"
                                            merge_confidence = "high"
                                            break
                                if target_id:
                                    break

                            elif cat == "concepts":
                                # 概念归并：确定性规则（全同/剥离括号后同） + 语义裁决（重合度 >= 40% 查 LLM 缓存/判定）
                                cur_types = info.get("types", [])
                                cur_type = cur_types[0] if cur_types else ""
                                exist_types = existing_ent.get("concept_types", [])
                                exist_type = exist_types[0] if exist_types else ""
                                if can_merge_concept_names(name, c_name, cur_type, exist_type):
                                    target_id = existing_ent["entity_id"]
                                    merge_rule_hit = "concept_semantic_match"
                                    merge_confidence = "high"
                                    break
                                for ex_alias in c_aliases:
                                    if can_merge_concept_names(name, ex_alias, cur_type, exist_type):
                                        target_id = existing_ent["entity_id"]
                                        merge_rule_hit = "concept_semantic_match"
                                        merge_confidence = "high"
                                        break
                                if target_id:
                                    break

                                # LLM 语义裁决（字符重合度 >= 40% 粗筛）
                                if is_judge_candidate(cat, name, c_name):
                                    judge = llm_judge
                                    if judge_same(judge, cat, name, c_name):
                                        target_id = existing_ent["entity_id"]
                                        merge_rule_hit = "llm_semantic_judge"
                                        merge_confidence = "high"
                                        break
                                for ex_alias in c_aliases:
                                    if is_judge_candidate(cat, name, ex_alias):
                                        judge = llm_judge
                                        if judge_same(judge, cat, name, ex_alias):
                                            target_id = existing_ent["entity_id"]
                                            merge_rule_hit = "llm_semantic_judge"
                                            merge_confidence = "high"
                                            break
                                if target_id:
                                    break

                            else:
                                # 其他类别（通用兜底规则）：
                                # 规则 2.1: 子串重合（统一粗筛闸门，60% 阈值；硬性拒绝规则已在迭代入口拦截）
                                if is_judge_candidate(cat, name, c_name, threshold=0.60):
                                    target_id = existing_ent["entity_id"]
                                    merge_rule_hit = "substring_overlap"
                                    merge_confidence = "high"
                                    break

                                # 检查是否与该实体的现有别名达到 60% 重合
                                for ex_alias in c_aliases:
                                    if is_judge_candidate(cat, name, ex_alias, threshold=0.60):
                                        target_id = existing_ent["entity_id"]
                                        merge_rule_hit = "substring_overlap"
                                        merge_confidence = "high"
                                        break
                                if target_id:
                                    break

                                # 规则 2.2: 模糊重合度在 40%-60% 之间，调用模型辅助判定（统一粗筛闸门 + 核心字重合度）
                                core_overlap = calculate_char_overlap_ratio(
                                    _strip_generic_tokens(name), _strip_generic_tokens(c_name)
                                )
                                if 0.40 <= core_overlap < 0.60 and is_judge_candidate(
                                    cat, name, c_name, threshold=0.40
                                ) and llm_callable is not None:
                                    if check_model_entity_similarity(name, c_name, cat, llm_callable=llm_callable):
                                        target_id = existing_ent["entity_id"]
                                        merge_rule_hit = "model_assisted"
                                        merge_confidence = "medium"
                                        break

                                # 规则 2.3: 频次主导（高频名称自动当 canonical_name，低频挂 aliases；频次差异 >= 2倍且包含核心字）
                                if is_judge_candidate(cat, name, c_name, threshold=0.35) and c_freq >= 2 * info["count"]:
                                    target_id = existing_ent["entity_id"]
                                    merge_rule_hit = "frequency_dominant"
                                    merge_confidence = "high"
                                    break

                    # 3) 执行并入或创建新实体
                    if target_id and target_id in canonical_by_id:
                        ent = canonical_by_id[target_id]
                        ent["total_frequency"] += info["count"]
                        ent["pages"] = sorted(list(set(ent.get("pages", []) + info["pages"])))
                        valid_ch = ent.setdefault("valid_chapters", [])
                        if chapter_id not in valid_ch:
                            valid_ch.append(chapter_id)
                        orig = ent.setdefault("original_names", [])
                        if name not in orig:
                            orig.append(name)
                        if cat == "concepts" and info.get("types"):
                            concept_types = ent.setdefault("concept_types", [])
                            for ct in info["types"]:
                                if ct not in concept_types:
                                    concept_types.append(ct)

                        # 如果当前名称是新别名，则挂入 aliases
                        if name != ent["canonical_name"] and name not in ent["aliases"]:
                            ent["aliases"].append(name)
                            ent.setdefault("alias_details", {})[name] = info["count"]
                            canonical_by_alias[(cat, name)] = target_id
                            if merge_rule_hit:
                                record_merge_log(name, ent["canonical_name"], merge_rule_hit, merge_confidence)

                        # 逆向频次主导（非 characters 类别）：如果本实体频次已经大幅反超现有 canonical_name（>= 2 倍），自动晋升为 canonical_name
                        if cat != "characters" and name != ent["canonical_name"] and info["count"] >= 2 * ent["total_frequency"] - 2 * info["count"]:
                            old_canon = ent["canonical_name"]
                            ent["canonical_name"] = name
                            if old_canon not in ent["aliases"]:
                                ent["aliases"].append(old_canon)
                            record_merge_log(old_canon, name, "frequency_dominant_promoted", "high")
                            canonical_by_name[(cat, name)] = target_id
                            canonical_by_alias[(cat, old_canon)] = target_id

                        if name not in updated_alias_map:
                            updated_alias_map[name] = target_id
                    else:
                        # 4) 生成 slug_id 并进行冲突检测
                        slug = to_romanized_slug(cat, name, slug_map=slug_map, slug_mode=effective_slug_mode)
                        base_slug_id = f"{CATEGORY_PREFIX[cat]}_{slug}"
                        final_slug_id = base_slug_id
                        counter = 2
                        while final_slug_id in canonical_by_id:
                            existing_ent = canonical_by_id[final_slug_id]
                            # 如果已有实体和当前新实体同名同类别，直接使用已有实体（防御性兜底）
                            if existing_ent["category"] == cat and existing_ent["canonical_name"] == name:
                                break
                            final_slug_id = f"{base_slug_id}_{counter}"
                            counter += 1

                        if final_slug_id in canonical_by_id:
                            # 命中了同名实体，执行并入
                            ent = canonical_by_id[final_slug_id]
                            ent["total_frequency"] += info["count"]
                            ent["pages"] = sorted(list(set(ent.get("pages", []) + info["pages"])))
                            valid_ch = ent.setdefault("valid_chapters", [])
                            if chapter_id not in valid_ch:
                                valid_ch.append(chapter_id)
                            orig = ent.setdefault("original_names", [])
                            if name not in orig:
                                orig.append(name)
                            if name not in updated_alias_map:
                                updated_alias_map[name] = final_slug_id
                        else:
                            # 创建全新实体
                            ent = {
                                "entity_id": final_slug_id,
                                "category": cat,
                                "canonical_name": name,
                                "confidence": "high",
                                "total_frequency": info["count"],
                                "pages": sorted(list(set(info["pages"]))),
                                "valid_chapters": [chapter_id],
                                "aliases": [],
                                "original_names": [name],
                                "alias_details": {},
                                "concept_types": list(info.get("types", [])) if cat == "concepts" else [],
                            }
                            canonical_by_id[final_slug_id] = ent
                            canonical_by_name[(cat, name)] = final_slug_id
                            if name not in updated_alias_map:
                                updated_alias_map[name] = final_slug_id

            # 5. 应用种子配置（全局层 global_terms 与章节层 chapter_overrides）
            for cat, terms in global_terms.items():
                for term, override_info in terms.items():
                    target_id = override_info.get("target_entity_id")
                    if target_id and target_id in canonical_by_id:
                        target_ent = canonical_by_id[target_id]
                        if term not in target_ent["aliases"]:
                            target_ent["aliases"].append(term)
                            record_merge_log(term, target_ent["canonical_name"], "seed_global_terms", "high")
                        orig = target_ent.setdefault("original_names", [])
                        if term not in orig:
                            orig.append(term)
                        updated_alias_map[term] = target_id

            for cat, terms in chapter_overrides.items():
                if isinstance(terms, Mapping):
                    for term, override_info in terms.items():
                        target_id = override_info.get("target_entity_id")
                        if target_id and target_id in canonical_by_id:
                            target_ent = canonical_by_id[target_id]
                            if term not in target_ent["aliases"]:
                                target_ent["aliases"].append(term)
                                record_merge_log(term, target_ent["canonical_name"], "seed_chapter_override", "high")
                            orig = target_ent.setdefault("original_names", [])
                            if term not in orig:
                                orig.append(term)
                            if term in raw_stats.get(cat, {}):
                                term_pages = raw_stats[cat][term]["pages"]
                                target_ent.setdefault("alias_details", {})[term] = f"{raw_stats[cat][term]['count']} (seed override)"
                                target_ent["pages"] = sorted(list(set(target_ent["pages"] + term_pages)))
                                target_ent["total_frequency"] += raw_stats[cat][term]["count"]
                            valid_ch = target_ent.setdefault("valid_chapters", [])
                            if chapter_id not in valid_ch:
                                valid_ch.append(chapter_id)
                            updated_alias_map[term] = target_id

            final_canonical_entities = list(canonical_by_id.values())
            for ent in final_canonical_entities:
                if "valid_chapters" not in ent or not ent["valid_chapters"]:
                    ent["valid_chapters"] = [chapter_id]
                else:
                    ent["valid_chapters"] = sorted(list(set(ent["valid_chapters"])))
                if "original_names" not in ent:
                    ent["original_names"] = [ent["canonical_name"]] + ent.get("aliases", [])
                ent["original_names"] = sorted(list(set(ent["original_names"])))
                # 词频语义：按页去重后的出现页数。历史产物按分析行累加造成的虚高（同一页被多个
                # run 的已发布结果重复喂入）在此一并纠正，保证 total_frequency == len(pages)。
                known_pages = [p for p in ent.get("pages", []) if p not in (None, "", 0)]
                if known_pages:
                    ent["pages"] = sorted(set(known_pages))
                    ent["total_frequency"] = len(ent["pages"])

            final_payload = {
                "canonical_entities": final_canonical_entities,
                "alias_map": updated_alias_map,
                "auto_merge_log": auto_merge_log,
                "slug_mode": effective_slug_mode,
                "version": current_revision + 1,
                "updated_at": utcnow().isoformat(),
                # 图组织与批量裁决的留痕：解释「本次为什么只合并了这些」，
                # 并保留低频/通用词被延迟的事实，供人工复核与后续重跑。
                "alignment_graph": alignment_summary,
                "alignment_metrics": alignment_stats,
                "alignment_blocked_merges": alignment_blocked[:50],
                "deferred_entities": alignment_deferred[:2000],
            }

            # 6. 写入数据库 analysis_artifacts
            now = utcnow()
            new_revision = current_revision + 1
            artifact_id = str(uuid.uuid4())

            if not staged:
                connection.execute(
                    update(analysis_artifacts)
                    .where(
                        analysis_artifacts.c.book_id == book_id,
                        analysis_artifacts.c.kind == "entity_alignment",
                        analysis_artifacts.c.template == "default",
                        analysis_artifacts.c.is_active.is_(True),
                    )
                    .values(is_active=False, updated_at=now)
                )

            connection.execute(
                insert(analysis_artifacts).values(
                    id=artifact_id,
                    book_id=book_id,
                    run_id=run_id,
                    kind="entity_alignment",
                    template="default",
                    status="building" if staged else "ready",
                    revision=new_revision,
                    is_active=not staged,
                    dependency_fingerprint=dependency_fingerprint
                    or hashlib.sha256(
                        _json(final_payload).encode("utf-8")
                    ).hexdigest(),
                    payload_json=_json(final_payload),
                    asset_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )

            return {
                "artifactId": artifact_id,
                "bookId": book_id,
                "revision": new_revision,
                "entityCount": len(final_canonical_entities),
                "aliasCount": len(updated_alias_map),
                "autoMergeCount": len(auto_merge_log),
                "staged": staged,
                "payload": final_payload,
            }

    def unmerge_alias(
        self,
        *,
        book_id: str,
        category: str,
        alias_to_detach: str,
        new_canonical_name: str | None = None,
        new_entity_id: str | None = None,
        slug_mode: str | None = None,
    ) -> dict[str, Any]:
        """手动回滚/拆分自动归并的别名，使其恢复为一个独立的标准实体。
        
        从现有包含 alias_to_detach 的实体中剥离该别名，并为其创建或生成全新的独立实体，
        同时更新 alias_map 并写入新 revision 的 entity_alignment 产物。
        """
        target_name = alias_to_detach.strip()
        new_canon = (new_canonical_name or target_name).strip()

        with self.engine.begin() as connection:
            self._assert_book_owned(connection, book_id)

            select_query = (
                select(analysis_artifacts)
                .where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind == "entity_alignment",
                    analysis_artifacts.c.template == "default",
                    analysis_artifacts.c.is_active.is_(True),
                )
            )
            try:
                select_query = select_query.with_for_update()
            except Exception:  # noqa: BLE001 - SQLite 不支持行锁，退化为无锁读取
                LOGGER.debug("当前数据库不支持 FOR UPDATE，读取退化为无锁", exc_info=True)

            existing_artifact = connection.execute(select_query).mappings().one_or_none()
            if existing_artifact is None:
                raise InsightNotFound("No active entity_alignment artifact found to unmerge")

            current_revision = existing_artifact["revision"]
            payload = _json_object(existing_artifact["payload_json"], "entity_alignment payload")
            canonical_entities = list(payload.get("canonical_entities", []))
            alias_map = dict(payload.get("alias_map", {}))
            auto_merge_log = list(payload.get("auto_merge_log", []))
            effective_slug_mode = slug_mode or payload.get("slug_mode") or "pinyin"
            if isinstance(effective_slug_mode, str) and effective_slug_mode.strip().lower() == "english":
                effective_slug_mode = "romanized"

            # 寻找拥有该别名的源实体
            source_entity: dict[str, Any] | None = None
            for ent in canonical_entities:
                if ent.get("category") == category and target_name in ent.get("aliases", []):
                    source_entity = ent
                    break

            if source_entity is None:
                raise InsightConflict(f"Alias '{target_name}' in category '{category}' not found in any entity's aliases")

            # 1. 从源实体中剥离该别名
            source_entity["aliases"] = [a for a in source_entity.get("aliases", []) if a != target_name]
            alias_det = source_entity.get("alias_details", {})
            detached_freq = 1
            if target_name in alias_det:
                raw_val = alias_det.pop(target_name)
                try:
                    detached_freq = int(str(raw_val).split()[0])
                except Exception:  # noqa: BLE001 - 别名频次格式异常时按 1 次处理
                    LOGGER.debug(
                        "别名频次无法解析，按 1 次处理: alias=%s value=%r",
                        target_name,
                        raw_val,
                    )
                    detached_freq = 1
            if "original_names" in source_entity:
                source_entity["original_names"] = [o for o in source_entity["original_names"] if o != target_name]

            # 拆分出的实体不能再声明源实体的全部出场页：pages 与 total_frequency 是
            # 同一批证据的两种投影，整份复制会让两个实体声明相同页面，图谱/列表页
            # 的共现统计随之翻倍失真。按拆分出的频次取前 N 页，并按页切分。
            detached_pages, remaining_pages = _split_entity_pages(
                source_entity.get("pages", []),
                detached_freq=detached_freq,
            )
            source_entity["pages"] = remaining_pages
            # pages 与 total_frequency 是同一批证据的两种投影，切页后必须一起归一，
            # 否则两个实体会各自声明「频次 N、页数 0」或重复声明同一页。
            source_entity["total_frequency"] = max(1, len(remaining_pages))
            detached_freq = max(1, len(detached_pages)) if detached_pages else detached_freq

            # 2. 为拆出来的实体生成新的 ID 与实体对象
            if new_entity_id:
                detached_id = new_entity_id
            else:
                slug = to_romanized_slug(category, new_canon, slug_mode=effective_slug_mode)
                detached_id = f"{CATEGORY_PREFIX[category]}_{slug}"

            detached_entity = {
                "entity_id": detached_id,
                "category": category,
                "canonical_name": new_canon,
                "confidence": "high",
                "total_frequency": detached_freq,
                "pages": detached_pages,
                "valid_chapters": list(source_entity.get("valid_chapters", [])),
                "aliases": [],
                "original_names": [target_name],
                "alias_details": {},
            }
            canonical_entities.append(detached_entity)

            # 3. 更新 alias_map 指向新实体
            alias_map[target_name] = detached_id
            if new_canon != target_name:
                alias_map[new_canon] = detached_id

            # 4. 记录回滚日志
            auto_merge_log.append({
                "raw_name": target_name,
                "canonical_name": new_canon,
                "rule": "manual_unmerge_rollback",
                "confidence": "high",
                "chapter_id": source_entity.get("valid_chapters", ["unknown"])[-1],
                "timestamp": utcnow().isoformat(),
            })

            new_revision = current_revision + 1
            now = utcnow()
            new_payload = {
                "canonical_entities": canonical_entities,
                "alias_map": alias_map,
                "auto_merge_log": auto_merge_log,
                "slug_mode": effective_slug_mode,
                "version": new_revision,
                "updated_at": now.isoformat(),
            }

            # 5. 持久化新 revision
            connection.execute(
                update(analysis_artifacts)
                .where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind == "entity_alignment",
                    analysis_artifacts.c.template == "default",
                    analysis_artifacts.c.is_active.is_(True),
                )
                .values(is_active=False, updated_at=now)
            )

            new_artifact_id = str(uuid.uuid4())
            connection.execute(
                insert(analysis_artifacts).values(
                    id=new_artifact_id,
                    book_id=book_id,
                    run_id=None,
                    kind="entity_alignment",
                    template="default",
                    status="ready",
                    revision=new_revision,
                    is_active=True,
                    dependency_fingerprint=hashlib.sha256(_json(new_payload).encode("utf-8")).hexdigest(),
                    payload_json=_json(new_payload),
                    asset_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )

            return {
                "artifactId": new_artifact_id,
                "bookId": book_id,
                "revision": new_revision,
                "detachedAlias": target_name,
                "newEntityId": detached_id,
                "payload": new_payload,
            }

    def list_pinyin_entities(
        self,
        *,
        book_id: str,
        category: str | None = None,
        mode: str | None = None,
    ) -> list[dict[str, Any]] | dict[str, list[dict[str, Any]]]:
        """获取当前活跃 entity_alignment 产物中所有带有未命中回退标记（如 '_pinyin_' 或 '_romanized_'）的实体清单。
        
        参数:
        - mode: "pinyin" 或 "romanized"（或 "english"）。
          - 若指定 mode，返回符合该模式的实体列表 list[dict]。
          - 若未指定 mode（默认同时检索），返回按模式区分的字典:
            {"pinyin": [...], "romanized": [...]}
        """
        with self.engine.connect() as connection:
            self._assert_book_owned(connection, book_id)
            row = connection.execute(
                select(analysis_artifacts.c.payload_json)
                .where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind == "entity_alignment",
                    analysis_artifacts.c.template == "default",
                    analysis_artifacts.c.is_active.is_(True),
                )
            ).mappings().one_or_none()

            if not row or not row["payload_json"]:
                if mode is not None:
                    return []
                return {"pinyin": [], "romanized": []}

            payload = _json_object(row["payload_json"], "entity_alignment payload")
            canonical_entities = payload.get("canonical_entities", [])
            
            pinyin_list = []
            romanized_list = []

            for ent in canonical_entities:
                if category and ent.get("category") != category:
                    continue
                ent_id = ent.get("entity_id", "")
                
                is_pinyin = "_pinyin_" in ent_id
                is_romanized = ("_romanized_" in ent_id) or ("_english_" in ent_id)

                if is_pinyin:
                    item = dict(ent)
                    item["mode"] = "pinyin"
                    pinyin_list.append(item)
                if is_romanized:
                    item = dict(ent)
                    item["mode"] = "romanized"
                    romanized_list.append(item)

            target_mode = mode.strip().lower() if isinstance(mode, str) else None
            if target_mode == "english":
                target_mode = "romanized"

            if target_mode == "pinyin":
                return pinyin_list
            elif target_mode == "romanized":
                return romanized_list
            else:
                return {
                    "pinyin": pinyin_list,
                    "romanized": romanized_list,
                }

    def get_entities(
        self,
        *,
        book_id: str,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """获取当前活跃 entity_alignment 产物中的所有实体列表。"""
        with self.engine.connect() as connection:
            self._assert_book_owned(connection, book_id)
            row = connection.execute(
                select(analysis_artifacts.c.payload_json)
                .where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind == "entity_alignment",
                    analysis_artifacts.c.template == "default",
                    analysis_artifacts.c.is_active.is_(True),
                )
            ).mappings().one_or_none()

            if not row or not row["payload_json"]:
                return []

            payload = _json_object(row["payload_json"], "entity_alignment payload")
            canonical_entities = payload.get("canonical_entities", [])

            results: list[dict[str, Any]] = []
            for ent in canonical_entities:
                if category and ent.get("category") != category:
                    continue
                results.append({
                    "entity_id": ent.get("entity_id", ""),
                    "canonical_name": ent.get("canonical_name", ""),
                    "category": ent.get("category", ""),
                    "aliases": ent.get("aliases", []),
                    "pages": ent.get("pages", []),
                    "frequency": ent.get("total_frequency", ent.get("frequency", 0)),
                })
            return results

    def get_graph_data(
        self,
        *,
        book_id: str,
        category: str | None = None,
        min_weight: int = 1,
    ) -> dict[str, Any]:
        """获取当前活跃 entity_alignment 产物中的实体关系图谱数据（含节点与页面共现边）。"""
        with self.engine.connect() as connection:
            self._assert_book_owned(connection, book_id)
            row = connection.execute(
                select(analysis_artifacts.c.payload_json)
                .where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind == "entity_alignment",
                    analysis_artifacts.c.template == "default",
                    analysis_artifacts.c.is_active.is_(True),
                )
            ).mappings().one_or_none()

            if not row or not row["payload_json"]:
                return {"nodes": [], "edges": []}

            payload = _json_object(row["payload_json"], "entity_alignment payload")
            canonical_entities = payload.get("canonical_entities", [])

            nodes: list[dict[str, Any]] = []
            entity_pages: dict[str, set[int]] = {}

            for ent in canonical_entities:
                cat = ent.get("category", "")
                if category and cat != category:
                    continue
                ent_id = ent.get("entity_id", "")
                if not ent_id:
                    continue
                pages = [
                    int(p)
                    for p in ent.get("pages", [])
                    if isinstance(p, (int, float)) or (isinstance(p, str) and str(p).isdigit())
                ]
                entity_pages[ent_id] = set(pages)
                nodes.append({
                    "id": ent_id,
                    "name": ent.get("canonical_name", ""),
                    "category": cat,
                    "aliases": ent.get("aliases", []),
                    "frequency": ent.get("total_frequency", ent.get("frequency", len(pages))),
                    "pages": sorted(pages),
                })

            edges: list[dict[str, Any]] = []
            node_ids = [n["id"] for n in nodes]
            n_len = len(node_ids)
            for i in range(n_len):
                id_a = node_ids[i]
                pages_a = entity_pages[id_a]
                if not pages_a:
                    continue
                for j in range(i + 1, n_len):
                    id_b = node_ids[j]
                    pages_b = entity_pages[id_b]
                    if not pages_b:
                        continue
                    common = sorted(pages_a & pages_b)
                    weight = len(common)
                    if weight >= min_weight:
                        edges.append({
                            "id": f"{id_a}--{id_b}",
                            "source": id_a,
                            "target": id_b,
                            "weight": weight,
                            "common_pages": common,
                        })

            return {"nodes": nodes, "edges": edges}

    def publish_timeline(
        self,
        *,
        connection: Connection,
        frozen: AnalysisInputSnapshot,
        result: Mapping[str, Any],
        activate: bool = True,
    ) -> dict[str, Any]:
        status = "building"
        if activate:
            current = self._snapshot(connection, book_id=frozen.book_id)
            status = _publication_status(frozen, current)
        mode = result.get("mode")
        if not isinstance(mode, str):
            raise InsightConflict("timeline mode is missing")
        if mode not in {"enhanced", "compressed", "simple"}:
            raise InsightConflict("timeline mode is invalid")
        try:
            content, raw_events, raw_characters = _validate_timeline_parts(
                content=result.get("content"),
                events=result.get("events"),
                characters=result.get("characters"),
                require_events=True,
            )
        except ValueError as exc:
            raise InsightConflict(str(exc)) from exc
        _validate_timeline_metadata(content, mode=mode)
        raw_events, raw_characters = _canonical_timeline_references(
            pages=frozen.pages,
            events=raw_events,
            characters=raw_characters,
        )
        timeline_id = str(uuid.uuid4())
        now = utcnow()
        should_activate = activate and status in {"ready", "degraded"}
        if should_activate:
            connection.execute(
                update(timeline_versions)
                .where(
                    timeline_versions.c.book_id == frozen.book_id,
                    timeline_versions.c.is_active.is_(True),
                )
                .values(is_active=False, updated_at=now)
            )
        connection.execute(
            insert(timeline_versions).values(
                id=timeline_id,
                book_id=frozen.book_id,
                run_id=frozen.source_run_id,
                mode=mode,
                status=status,
                content_json=_json(content),
                dependency_fingerprint=frozen.fingerprint,
                is_active=should_activate,
                created_at=now,
                updated_at=now,
            )
        )
        if raw_events:
            connection.execute(
                insert(timeline_events),
                [
                    {
                        "id": str(uuid.uuid4()),
                        "timeline_version_id": timeline_id,
                        "ordinal": index,
                        "payload_json": _json(event),
                    }
                    for index, event in enumerate(raw_events, start=1)
                ],
            )
        character_rows = []
        for character in raw_characters:
            name = character["name"]
            character_rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "timeline_version_id": timeline_id,
                    "name": name,
                    "payload_json": _json(character),
                }
            )
        if character_rows:
            connection.execute(insert(timeline_characters), character_rows)
        return {
            "timelineVersionId": timeline_id,
            "mode": mode,
            "status": status,
            "eventCount": len(raw_events),
            "characterCount": len(character_rows),
        }

    def next_vector_generation(self, book_id: str) -> int:
        with self.engine.connect() as connection:
            return _required_integer(
                connection.execute(
                    select(
                        func.coalesce(
                            func.max(vector_generations.c.generation),
                            0,
                        )
                        + 1
                    ).where(vector_generations.c.book_id == book_id)
                ).scalar_one(),
                "next vector generation",
                minimum=1,
            )

    def checkpoint_vector_generation(
        self,
        *,
        connection: Connection,
        frozen: AnalysisInputSnapshot,
        generation: int,
        page_count: int,
        event_count: int,
    ) -> dict[str, Any]:
        generation = _required_integer(
            generation,
            "vector generation",
            minimum=1,
        )
        page_count = _required_integer(page_count, "vector page count")
        event_count = _required_integer(event_count, "vector event count")
        row = connection.execute(
            select(vector_generations).where(
                vector_generations.c.book_id == frozen.book_id,
                vector_generations.c.generation == generation,
            )
        ).mappings().one_or_none()
        now = utcnow()
        if row is None:
            generation_id = str(uuid.uuid4())
            connection.execute(
                insert(vector_generations).values(
                    id=generation_id,
                    book_id=frozen.book_id,
                    run_id=frozen.source_run_id,
                    generation=generation,
                    status="building",
                    dependency_fingerprint=frozen.fingerprint,
                    page_count=page_count,
                    event_count=event_count,
                    is_active=False,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            if (
                row["run_id"] != frozen.source_run_id
                or row["dependency_fingerprint"] != frozen.fingerprint
                or _required_boolean(
                    row["is_active"],
                    "vector generation active flag",
                )
                or row["status"] != "building"
                or page_count
                < _required_integer(row["page_count"], "stored vector page count")
                or event_count
                < _required_integer(row["event_count"], "stored vector event count")
            ):
                raise InsightConflict("vector generation checkpoint conflicts")
            generation_id = _required_string(row["id"], "vector generation id")
            connection.execute(
                update(vector_generations)
                .where(vector_generations.c.id == generation_id)
                .values(
                    page_count=page_count,
                    event_count=event_count,
                    updated_at=now,
                )
            )
        return {
            "vectorGenerationId": generation_id,
            "generation": generation,
            "status": "building",
            "pageCount": page_count,
            "eventCount": event_count,
        }

    def fail_vector_generation(self, *, book_id: str, generation: int) -> None:
        book_id = _required_string(book_id, "vector bookId")
        generation = _required_integer(
            generation,
            "vector generation",
            minimum=1,
        )
        with self.engine.begin() as connection:
            connection.execute(
                update(vector_generations)
                .where(
                    vector_generations.c.book_id == book_id,
                    vector_generations.c.generation == generation,
                    vector_generations.c.status == "building",
                    vector_generations.c.is_active.is_(False),
                )
                .values(status="failed", updated_at=utcnow())
            )

    def publish_vector_generation(
        self,
        *,
        connection: Connection,
        frozen: AnalysisInputSnapshot,
        generation: int,
        page_count: int,
        event_count: int,
        activate: bool = True,
    ) -> dict[str, Any]:
        generation = _required_integer(
            generation,
            "vector generation",
            minimum=1,
        )
        page_count = _required_integer(page_count, "vector page count")
        event_count = _required_integer(event_count, "vector event count")
        status = "building"
        if activate:
            current = self._snapshot(connection, book_id=frozen.book_id)
            status = _publication_status(frozen, current)
        now = utcnow()
        should_activate = activate and status in {"ready", "degraded"}
        if should_activate:
            connection.execute(
                update(vector_generations)
                .where(
                    vector_generations.c.book_id == frozen.book_id,
                    vector_generations.c.is_active.is_(True),
                )
                .values(is_active=False, updated_at=now)
            )
        existing = connection.execute(
            select(vector_generations).where(
                vector_generations.c.book_id == frozen.book_id,
                vector_generations.c.generation == generation,
            )
        ).mappings().one_or_none()
        if existing is None:
            generation_id = str(uuid.uuid4())
            connection.execute(
                insert(vector_generations).values(
                    id=generation_id,
                    book_id=frozen.book_id,
                    run_id=frozen.source_run_id,
                    generation=generation,
                    status=status,
                    dependency_fingerprint=frozen.fingerprint,
                    page_count=page_count,
                    event_count=event_count,
                    is_active=should_activate,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            if (
                existing["run_id"] != frozen.source_run_id
                or existing["dependency_fingerprint"] != frozen.fingerprint
                or _required_boolean(
                    existing["is_active"],
                    "vector generation active flag",
                )
                or existing["status"] != "building"
            ):
                raise InsightConflict("vector generation publication conflicts")
            generation_id = _required_string(
                existing["id"],
                "vector generation id",
            )
            connection.execute(
                update(vector_generations)
                .where(vector_generations.c.id == generation_id)
                .values(
                    status=status,
                    page_count=page_count,
                    event_count=event_count,
                    is_active=should_activate,
                    updated_at=now,
                )
            )
        return {
            "vectorGenerationId": generation_id,
            "generation": generation,
            "status": status,
            "pageCount": page_count,
            "eventCount": event_count,
        }

    def get_artifact(
        self,
        *,
        book_id: str,
        kind: str,
        template: str,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            self._assert_book_owned(connection, book_id)
            row = connection.execute(
                select(analysis_artifacts).where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind == kind,
                    analysis_artifacts.c.template == template,
                    analysis_artifacts.c.is_active.is_(True),
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        artifact_kind = _required_string(row["kind"], "Insight artifact kind")
        artifact_template = _required_string(
            row["template"],
            "Insight artifact template",
        )
        if artifact_kind not in {"overview", "compressed_context", "entity_alignment"}:
            raise InsightConflict(
                "active Insight artifact kind is invalid"
            )
        if (
            artifact_kind == "overview"
            and artifact_template not in OVERVIEW_TEMPLATES
        ) or (
            artifact_kind in {"compressed_context", "entity_alignment"}
            and artifact_template != "default"
        ):
            raise InsightConflict(
                "active Insight artifact template is invalid"
            )
        status = _required_string(row["status"], "Insight artifact status")
        if status not in {"ready", "degraded", "stale"}:
            raise InsightConflict(
                "active Insight artifact status is invalid"
            )
        try:
            payload = validate_artifact_payload(
                kind=artifact_kind,
                template=artifact_template,
                payload=_json_object(
                    row["payload_json"],
                    "analysis artifact payload",
                ),
            )
        except InsightConflict as exc:
            raise InsightConflict(
                "active Insight artifact payload is invalid"
            ) from exc
        return {
            "artifactId": _required_string(row["id"], "Insight artifact id"),
            "bookId": _required_string(row["book_id"], "Insight artifact bookId"),
            "runId": _optional_string(row["run_id"], "Insight artifact runId"),
            "kind": artifact_kind,
            "template": artifact_template,
            "status": status,
            "revision": _required_integer(
                row["revision"],
                "Insight artifact revision",
                minimum=1,
            ),
            "dependencyFingerprint": _required_sha256(
                row["dependency_fingerprint"],
                "Insight artifact dependency fingerprint",
            ),
            "payload": payload,
        }

    def get_timeline(
        self,
        *,
        book_id: str,
        event_after: int = 0,
        event_limit: int = 100,
        character_after: str | None = None,
        character_limit: int = 100,
    ) -> dict[str, Any] | None:
        if isinstance(event_after, bool) or not isinstance(event_after, int) or event_after < 0:
            raise ValueError("event cursor must be nonnegative")
        if isinstance(event_limit, bool) or not isinstance(event_limit, int) or not 1 <= event_limit <= 200:
            raise ValueError("event limit must be between 1 and 200")
        if (
            isinstance(character_limit, bool)
            or not isinstance(character_limit, int)
            or not 1 <= character_limit <= 200
        ):
            raise ValueError("character limit must be between 1 and 200")
        if character_after is not None:
            _required_string(character_after, "character cursor")
        with self.engine.connect() as connection:
            self._assert_book_owned(connection, book_id)
            row = connection.execute(
                select(timeline_versions).where(
                    timeline_versions.c.book_id == book_id,
                    timeline_versions.c.is_active.is_(True),
                )
            ).mappings().one_or_none()
            if row is None:
                return None
            timeline_id = _required_string(row["id"], "stored timeline id")
            mode = _required_string(row["mode"], "stored timeline mode")
            if mode not in {"enhanced", "compressed", "simple"}:
                raise InsightInvariant(
                    "stored timeline mode is invalid"
                )
            status = _required_string(row["status"], "stored timeline status")
            if status not in {"ready", "degraded", "stale"}:
                raise InsightInvariant(
                    "stored timeline status is invalid"
                )
            event_rows = list(
                connection.execute(
                    select(
                        timeline_events.c.id,
                        timeline_events.c.ordinal,
                        timeline_events.c.payload_json,
                    )
                    .where(
                        timeline_events.c.timeline_version_id == timeline_id,
                        timeline_events.c.ordinal > event_after,
                    )
                    .order_by(timeline_events.c.ordinal)
                    .limit(event_limit + 1)
                )
            )
            character_statement = select(
                timeline_characters.c.id,
                timeline_characters.c.name,
                timeline_characters.c.payload_json,
            ).where(
                timeline_characters.c.timeline_version_id == timeline_id
            )
            if character_after:
                character_statement = character_statement.where(
                    timeline_characters.c.name > character_after
                )
            character_rows = list(
                connection.execute(
                    character_statement.order_by(
                        timeline_characters.c.name
                    ).limit(character_limit + 1)
                )
            )
            event_count = _required_integer(
                connection.execute(
                    select(func.count(timeline_events.c.id)).where(
                        timeline_events.c.timeline_version_id == timeline_id
                    )
                ).scalar_one(),
                "stored timeline event count",
            )
            character_count = _required_integer(
                connection.execute(
                    select(func.count(timeline_characters.c.id)).where(
                        timeline_characters.c.timeline_version_id == timeline_id
                    )
                ).scalar_one(),
                "stored timeline character count",
            )
            page_count = _required_integer(
                connection.execute(
                    select(func.count(pages.c.id))
                    .join(chapters, chapters.c.id == pages.c.chapter_id)
                    .where(chapters.c.book_id == book_id)
                ).scalar_one(),
                "stored timeline page count",
            )
            has_more_events = len(event_rows) > event_limit
            selected_events = event_rows[:event_limit]
            has_more_characters = len(character_rows) > character_limit
            selected_characters = character_rows[:character_limit]
            content_payload = _json_object(
                row["content_json"],
                "timeline content",
            )
            event_payloads = []
            for event_id, _ordinal, value in selected_events:
                event_payload = _json_object(value, "timeline event payload")
                if "eventId" in event_payload:
                    raise InsightInvariant(
                        "stored timeline event contains a reserved id"
                    )
                event_payloads.append(
                    {
                        **event_payload,
                        "eventId": _required_string(
                            event_id,
                            "stored timeline event id",
                        ),
                    }
                )
            character_payloads = []
            for character_id, _name, value in selected_characters:
                character_payload = _json_object(
                    value,
                    "timeline character payload",
                )
                if "characterId" in character_payload:
                    raise InsightInvariant(
                        "stored timeline character contains a reserved id"
                    )
                character_payloads.append(
                    {
                        **character_payload,
                        "characterId": _required_string(
                            character_id,
                            "stored timeline character id",
                        ),
                    }
                )
            try:
                content_payload, event_payloads, character_payloads = _validate_timeline_parts(
                    content=content_payload,
                    events=event_payloads,
                    characters=character_payloads,
                    require_events=False,
                )
                _validate_timeline_metadata(
                    content_payload,
                    mode=mode,
                )
            except ValueError as exc:
                raise InsightInvariant(
                    "stored timeline payload is invalid"
                ) from exc
            referenced_page_ids: set[str] = set()
            for payload in event_payloads:
                page_ids = payload.get("page_ids")
                page_numbers = payload.get("page_numbers")
                if not isinstance(page_ids, list) or not page_ids or any(
                    not isinstance(page_id, str) or not page_id
                    for page_id in page_ids
                ):
                    raise InsightInvariant(
                        "stored timeline event page_ids are invalid"
                    )
                if (
                    not isinstance(page_numbers, list)
                    or not page_numbers
                    or len(page_numbers) != len(page_ids)
                ):
                    raise InsightInvariant(
                        "stored timeline event page references are incomplete"
                    )
                referenced_page_ids.update(page_ids)
            referenced_page_numbers = _timeline_thumbnail_page_numbers(
                content=content_payload,
                events=event_payloads,
                characters=character_payloads,
            )
            page_thumbnails: dict[str, str] = {}
            page_sources: dict[str, str] = {}
            if referenced_page_ids or referenced_page_numbers:
                thumbnail_pointer = page_assets.alias(
                    "timeline_thumbnail_pointer"
                )
                source_pointer = page_assets.alias("timeline_source_pointer")
                numbered_pages = (
                    select(
                        pages.c.id.label("page_id"),
                        func.row_number()
                        .over(order_by=(chapters.c.ordinal, pages.c.ordinal))
                        .label("page_number"),
                        thumbnail_pointer.c.asset_id.label(
                            "thumbnail_asset_id"
                        ),
                        source_pointer.c.asset_id.label("source_asset_id"),
                    )
                    .join(chapters, chapters.c.id == pages.c.chapter_id)
                    .outerjoin(
                        thumbnail_pointer,
                        (
                            thumbnail_pointer.c.page_id == pages.c.id
                        )
                        & (
                            thumbnail_pointer.c.role == "thumbnail_source"
                        ),
                    )
                    .outerjoin(
                        source_pointer,
                        (source_pointer.c.page_id == pages.c.id)
                        & (source_pointer.c.role == "source"),
                    )
                    .where(chapters.c.book_id == book_id)
                    .subquery()
                )
                page_filter = (
                    numbered_pages.c.page_id.in_(referenced_page_ids)
                    if referenced_page_ids
                    else numbered_pages.c.page_number.in_(
                        referenced_page_numbers
                    )
                )
                if referenced_page_ids and referenced_page_numbers:
                    page_filter = page_filter | numbered_pages.c.page_number.in_(
                        referenced_page_numbers
                    )
                page_rows = list(
                    connection.execute(
                        select(
                            numbered_pages.c.page_id,
                            numbered_pages.c.page_number,
                            numbered_pages.c.thumbnail_asset_id,
                            numbered_pages.c.source_asset_id,
                        ).where(page_filter)
                    ).mappings()
                )
                page_numbers_by_id: dict[str, int] = {}
                page_thumbnails = {}
                page_sources = {}
                for page in page_rows:
                    page_id = _required_string(
                        page["page_id"],
                        "stored timeline page id",
                    )
                    page_number = _required_integer(
                        page["page_number"],
                        "stored timeline page number",
                        minimum=1,
                    )
                    if page_id in page_numbers_by_id:
                        raise InsightInvariant(
                            "stored timeline page reference is duplicated"
                        )
                    page_numbers_by_id[page_id] = page_number
                    thumbnail_asset_id = page["thumbnail_asset_id"]
                    if thumbnail_asset_id is not None:
                        page_thumbnails[f"{page_number}"] = (
                            "/api/v2/assets/"
                            + _required_string(
                                thumbnail_asset_id,
                                "stored timeline thumbnail asset id",
                            )
                        )
                    source_asset_id = page["source_asset_id"]
                    if source_asset_id is not None:
                        page_sources[f"{page_number}"] = (
                            "/api/v2/assets/"
                            + _required_string(
                                source_asset_id,
                                "stored timeline source asset id",
                            )
                        )
                for payload in event_payloads:
                    if [
                        page_numbers_by_id.get(page_id)
                        for page_id in payload["page_ids"]
                    ] != payload["page_numbers"]:
                        raise InsightInvariant(
                            "stored timeline page references are stale or invalid"
                        )
        return {
            "timelineVersionId": timeline_id,
            "bookId": _required_string(row["book_id"], "stored timeline bookId"),
            "runId": _optional_string(row["run_id"], "stored timeline runId"),
            "mode": mode,
            "status": status,
            "content": content_payload,
            "events": event_payloads,
            "characters": character_payloads,
            "eventPage": {
                "totalCount": event_count,
                "nextCursor": (
                    _required_integer(
                        selected_events[-1][1],
                        "stored timeline event cursor",
                        minimum=0,
                    )
                    if has_more_events and selected_events
                    else None
                )
            },
            "characterPage": {
                "totalCount": character_count,
                "nextCursor": (
                    _required_string(
                        selected_characters[-1][1],
                        "stored timeline character cursor",
                    )
                    if has_more_characters and selected_characters
                    else None
                )
            },
            "pageCount": page_count,
            "pageThumbnails": page_thumbnails,
            "pageSources": page_sources,
            "dependencyFingerprint": _required_sha256(
                row["dependency_fingerprint"],
                "stored timeline dependency fingerprint",
            ),
        }

    def get_timeline_status(
        self,
        *,
        book_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            self._assert_book_owned(connection, book_id)
            row = connection.execute(
                select(
                    timeline_versions.c.id,
                    timeline_versions.c.mode,
                    timeline_versions.c.status,
                ).where(
                    timeline_versions.c.book_id == book_id,
                    timeline_versions.c.is_active.is_(True),
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        mode = _required_string(row["mode"], "stored timeline mode")
        if mode not in {"enhanced", "compressed", "simple"}:
            raise InsightInvariant(
                "stored timeline mode is invalid"
            )
        status = _required_string(row["status"], "stored timeline status")
        if status not in {"ready", "degraded", "stale"}:
            raise InsightInvariant(
                "stored timeline status is invalid"
            )
        return {
            "timelineVersionId": _required_string(
                row["id"], "stored timeline id"
            ),
            "mode": mode,
            "status": status,
        }

    def list_timeline_characters(
        self,
        *,
        book_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            self._assert_book_owned(connection, book_id)
            timeline = connection.execute(
                select(
                    timeline_versions.c.id,
                    timeline_versions.c.mode,
                    timeline_versions.c.status,
                ).where(
                    timeline_versions.c.book_id == book_id,
                    timeline_versions.c.is_active.is_(True),
                )
            ).mappings().one_or_none()
            if timeline is None:
                return None
            rows = connection.execute(
                select(
                    timeline_characters.c.id,
                    timeline_characters.c.payload_json,
                )
                .where(
                    timeline_characters.c.timeline_version_id
                    == timeline["id"]
                )
                .order_by(timeline_characters.c.name)
            ).mappings()
            characters = []
            for row in rows:
                payload = _json_object(
                    row["payload_json"],
                    "timeline character payload",
                )
                if "characterId" in payload:
                    raise InsightInvariant(
                        "stored timeline character contains a reserved id"
                    )
                characters.append(
                    {
                        **payload,
                        "characterId": _required_string(
                            row["id"],
                            "stored timeline character id",
                        ),
                    }
                )
        mode = _required_string(timeline["mode"], "stored timeline mode")
        if mode not in {"enhanced", "compressed", "simple"}:
            raise InsightInvariant(
                "stored timeline mode is invalid"
            )
        status = _required_string(timeline["status"], "stored timeline status")
        if status not in {"ready", "degraded", "stale"}:
            raise InsightInvariant(
                "stored timeline status is invalid"
            )
        try:
            _, _, characters = _validate_timeline_parts(
                content={},
                events=[],
                characters=characters,
                require_events=False,
            )
        except ValueError as exc:
            raise InsightInvariant(
                "stored timeline character payload is invalid"
            ) from exc
        return {
            "timelineVersionId": _required_string(
                timeline["id"], "stored timeline id"
            ),
            "mode": mode,
            "status": status,
            "characters": characters,
        }

    def get_timeline_character(
        self,
        *,
        book_id: str,
        character_id: str,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            self._assert_book_owned(connection, book_id)
            row = connection.execute(
                select(
                    timeline_versions.c.id.label("timeline_version_id"),
                    timeline_versions.c.mode,
                    timeline_versions.c.status,
                    timeline_characters.c.id.label("character_id"),
                    timeline_characters.c.payload_json,
                )
                .join(
                    timeline_characters,
                    timeline_characters.c.timeline_version_id
                    == timeline_versions.c.id,
                )
                .where(
                    timeline_versions.c.book_id == book_id,
                    timeline_versions.c.is_active.is_(True),
                    timeline_characters.c.id == character_id,
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        mode = _required_string(row["mode"], "stored timeline mode")
        if mode not in {"enhanced", "compressed", "simple"}:
            raise InsightInvariant(
                "stored timeline mode is invalid"
            )
        status = _required_string(row["status"], "stored timeline status")
        if status not in {"ready", "degraded", "stale"}:
            raise InsightInvariant(
                "stored timeline status is invalid"
            )
        payload = _json_object(
            row["payload_json"],
            "timeline character payload",
        )
        if "characterId" in payload:
            raise InsightInvariant(
                "stored timeline character contains a reserved id"
            )
        character = {
            **payload,
            "characterId": _required_string(
                row["character_id"],
                "stored timeline character id",
            ),
        }
        try:
            _, _, characters = _validate_timeline_parts(
                content={},
                events=[],
                characters=[character],
                require_events=False,
            )
        except ValueError as exc:
            raise InsightInvariant(
                "stored timeline character payload is invalid"
            ) from exc
        return {
            "timelineVersionId": _required_string(
                row["timeline_version_id"],
                "stored timeline id",
            ),
            "mode": mode,
            "status": status,
            "character": characters[0],
        }

    def qa_status(
        self,
        *,
        book_id: str,
        mode: str = "exact",
    ) -> dict[str, Any]:
        if mode not in {"generic", "exact", "global"}:
            raise ValueError("mode must be generic, exact or global")
        with self.engine.connect() as connection:
            self._assert_book_owned(connection, book_id)
            try:
                current = self._snapshot(connection, book_id=book_id)
            except InsightNotFound:
                return {
                    "available": False,
                    "reason": "analysis_missing",
                    "repairAction": "analyze",
                }
        if mode == "global":
            return {
                "available": True,
                "reason": None,
            }
        # generic 与 exact 共用同一套页面/向量可用性判定。
        with self.engine.connect() as connection:
            vector = connection.execute(
                select(vector_generations).where(
                    vector_generations.c.book_id == book_id,
                    vector_generations.c.is_active.is_(True),
                )
            ).mappings().one_or_none()
        vector_generation = 0
        coverage = None
        if vector is not None:
            try:
                vector_status = _required_string(vector["status"], "vector status")
                dependency_fingerprint = _required_sha256(
                    vector["dependency_fingerprint"],
                    "vector dependency fingerprint",
                )
                if (
                    vector_status in {"ready", "degraded"}
                    and dependency_fingerprint == current.fingerprint
                ):
                    vector_generation = _required_integer(
                        vector["generation"],
                        "vector generation",
                        minimum=1,
                    )
                    coverage = {
                        "pages": _required_integer(
                            vector["page_count"],
                            "vector page count",
                        ),
                        "events": _required_integer(
                            vector["event_count"],
                            "vector event count",
                        ),
                    }
            except Exception:
                vector_generation = 0
                coverage = None
        result: dict[str, Any] = {
            "available": True,
            "reason": None,
            "generation": vector_generation,
        }
        if coverage is not None:
            result["coverage"] = coverage
        return result


def _slug_mode_from_config(config: Mapping[str, Any]) -> str | None:
    """从冻结的 Insight 配置中读取 slug 模式（analysis.slugMode 为权威，根层为兼容别名）。"""
    analysis_config = config.get("analysis")
    if isinstance(analysis_config, Mapping):
        mode = analysis_config.get("slugMode") or analysis_config.get("slug_mode")
        if isinstance(mode, str) and mode.strip():
            return mode.strip().lower()
    mode = config.get("slugMode") or config.get("slug_mode")
    if isinstance(mode, str) and mode.strip():
        return mode.strip().lower()
    return None


class InsightDerivedCommandService:
    def __init__(self, engine: Engine) -> None:
        self.jobs = JobQueueRepository(engine)
        self.settings = SettingsResolver(engine)
        self.repository = InsightDerivedRepository(engine)

    def create_job(
        self,
        *,
        book_id: str,
        kind: str,
        template: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        book_id = _required_string(book_id, "Insight bookId")
        if kind not in DERIVED_KINDS:
            raise ValueError("unsupported Insight derived kind")
        if kind == "overview":
            if template not in OVERVIEW_TEMPLATES:
                raise ValueError("unsupported Insight overview template")
        elif template != "default":
            raise ValueError(f"{kind} template must be default")
        frozen = self.repository.snapshot(book_id=book_id)
        config = self.settings.resolve_insight(
            book_id=book_id,
            scope="full",
        )
        config.update(
            {
                "bookId": book_id,
                "derivedKind": kind,
                "template": template,
                "sourceRunId": frozen.source_run_id,
                "sourceRunStatus": frozen.source_run_status,
                "analysisInputs": [
                    {
                        "resultId": page["resultId"],
                        "pageId": page["pageId"],
                        "pageNumber": page["pageNumber"],
                        "currentSourceChecksum": page[
                            "currentSourceChecksum"
                        ],
                    }
                    for page in frozen.pages
                ],
                "analysisInputFingerprint": frozen.fingerprint,
            }
        )
        step = {
            "overview": "insight_build_overview",
            "compressed_context": "insight_build_compressed_context",
            "timeline": "insight_build_timeline",
            "vector": "insight_build_vectors",
            "entity_alignment": "insight_build_entity_alignment",
        }[kind]
        job_kind = "vector_rebuild" if kind == "vector" else "derived_rebuild"
        return self.jobs.create_batch(
            display_name=f"Insight · {kind}",
            specs=(
                JobSpec(
                    kind=job_kind,
                    book_id=book_id,
                    analysis_run_id=frozen.source_run_id,
                    config=config,
                    items=(
                        JobItemSpec(
                            page_id=None,
                            step_kinds=(step,),
                        ),
                    ),
                    target_display={
                        "bookId": book_id,
                        "kind": kind,
                        "template": template,
                    },
                ),
            ),
            idempotency_scope=f"insight-derived:{book_id}:{kind}:{template}",
            idempotency_key=idempotency_key,
            idempotency_payload={
                "bookId": book_id,
                "kind": kind,
                "template": template,
                "fingerprint": frozen.fingerprint,
            },
        )


class InsightDerivedWorkerService:
    def __init__(
        self,
        *,
        data_root: Path,
        engine: Engine,
        jobs: JobQueueRepository,
        algorithms: DerivedAlgorithms | None = None,
        vector_store: InsightVectorStore | None = None,
    ) -> None:
        self.engine = engine
        self.jobs = jobs
        self.repository = InsightDerivedRepository(engine)
        self.credentials = SettingsRepository(engine)
        self.algorithms = algorithms or ProviderDerivedAlgorithms()
        self.vector_store = vector_store or InsightVectorStore(data_root)
        self.entity_slug_map = load_entity_slug_map(data_root / ENTITY_SLUG_MAP_FILENAME)
        self.data_root = Path(data_root)

    def handle(
        self,
        fence: AttemptFence,
        step: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raw_config = _required_mapping(
            step.get("config"),
            "frozen Insight job config",
        )
        kind = _required_string(step.get("stepKind"), "Insight step kind")
        step_id = _required_string(step.get("stepId"), "Insight stepId")
        layer_match = re.fullmatch(
            r"insight_build_layer_(0|[1-9][0-9]*)",
            kind,
        )
        if kind in {"insight_build_vectors", "insight_stage_vectors"}:
            credential_sections = ("embedding",)
        elif (
            layer_match is not None
            or kind
            in {
                "insight_build_overview",
                "insight_stage_overview_no_spoiler",
                "insight_stage_overview_story_summary",
                "insight_build_compressed_context",
                "insight_stage_compressed_context",
                "insight_build_timeline",
                "insight_stage_timeline",
                "insight_stage_entity_alignment",
                "insight_build_entity_alignment",
            }
        ):
            credential_sections = ("chat",)
        else:
            raise JobConflict(f"unsupported derived step: {kind}")
        config = self._with_credentials(
            raw_config,
            section_names=credential_sections,
        )
        book_id = _required_string(config.get("bookId"), "Insight bookId")
        run_id_value = config.get("runId")
        run_id = (
            _required_string(run_id_value, "Insight runId")
            if run_id_value is not None
            else None
        )
        scope = _required_string(config.get("scope"), "Insight scope")
        if scope not in {"full", "chapter", "page", "incremental", "derived"}:
            raise JobConflict("frozen Insight scope is invalid")
        if layer_match is not None and run_id is None:
            raise JobConflict("summary layer step is missing its analysis run")
        staging_run = (
            run_id is not None
            and (
                layer_match is not None
                or kind.startswith("insight_stage_")
            )
        )
        if staging_run:
            frozen = self.repository.snapshot_for_run(run_id=run_id)
        else:
            frozen_inputs = config.get("analysisInputs")
            if (
                not book_id
                or not isinstance(frozen_inputs, list)
                or not all(
                    isinstance(value, Mapping) for value in frozen_inputs
                )
            ):
                raise JobConflict(
                    "derived job has an invalid frozen input snapshot"
                )
            frozen = self.repository.snapshot(
                book_id=book_id,
                frozen_inputs=frozen_inputs,
            )
            expected = _required_sha256(
                config.get("analysisInputFingerprint"),
                "Insight analysisInputFingerprint",
            )
            if frozen.fingerprint != expected:
                raise JobConflict(
                    "frozen Insight input fingerprint is invalid"
                )
            source_run_id = config.get("sourceRunId")
            source_run_status = config.get("sourceRunStatus")
            if source_run_id is not None:
                source_run_id = _required_string(
                    source_run_id,
                    "Insight sourceRunId",
                )
            if source_run_status is not None:
                source_run_status = _required_string(
                    source_run_status,
                    "Insight sourceRunStatus",
                )
                if source_run_status not in FINAL_ANALYSIS_RUN_STATUSES:
                    raise JobConflict("frozen Insight source run status is invalid")
            if (source_run_id is None) != (source_run_status is None):
                raise JobConflict(
                    "frozen Insight source run identity is incomplete"
                )
            frozen = AnalysisInputSnapshot(
                book_id=frozen.book_id,
                source_run_id=source_run_id,
                source_run_status=source_run_status,
                result_ids=frozen.result_ids,
                pages=frozen.pages,
                fingerprint=frozen.fingerprint,
            )
        try:
            if layer_match is not None:
                if run_id is None:
                    raise JobConflict(
                        "summary layer step is missing its analysis run"
                    )
                layer_index = int(layer_match.group(1))
                layer_units = self.repository.layer_units(
                    run_id=run_id,
                    layer_index=layer_index,
                    config=config,
                )
                if not layer_units:
                    raise InsightConflict("Insight layer has no units")
                layer_definition = _required_mapping(
                    layer_units[0].get("layer"),
                    "frozen Insight layer definition",
                )
                layer_name = _required_string(
                    layer_definition.get("name"),
                    "frozen Insight layer name",
                )
                with self.repository.engine.connect() as connection:
                    completed_indices = set(connection.execute(
                        select(analysis_layer_results.c.unit_index).where(
                            analysis_layer_results.c.run_id == run_id,
                            analysis_layer_results.c.layer_index == layer_index,
                        )
                    ).scalars())
                failed_units = []
                for unit in layer_units:
                    self.jobs.assert_attempt_active(fence)
                    if unit["unitIndex"] in completed_indices:
                        continue
                    try:
                        content = self.algorithms.build_layer(
                            unit["inputs"], layer=unit["layer"], config=config,
                        )
                        if not isinstance(content, Mapping) or not contains_nonempty_text(content):
                            raise InsightConflict(
                                "summary layer algorithm returned an empty or non-object result"
                            )
                    except AttemptFenced:
                        raise
                    except Exception as exc:
                        failed_units.append({
                            "unitIndex": unit["unitIndex"],
                            "error": redact_sensitive_text(str(exc)),
                        })
                        continue
                    completed = {**unit, "content": dict(content)}
                    self.jobs.checkpoint_step(
                        fence, step_id=step_id,
                        checkpoint={"completedUnitCount": len(completed_indices) + 1},
                        publisher=lambda connection: self.repository.publish_layer(
                            connection, run_id=run_id, layer_index=layer_index,
                            layer_name=layer_name, units=[completed],
                        ),
                    )
                    completed_indices.add(unit["unitIndex"])
                    log_result(
                        f"第 {layer_index + 1} 层第 {unit['unitIndex'] + 1} 组汇总已保存",
                        json_details(content),
                    )
                if failed_units:
                    self.jobs.checkpoint_step(
                        fence, step_id=step_id,
                        checkpoint={"completedUnitCount": len(completed_indices), "failedUnits": failed_units},
                    )
                    self.jobs.fail_step(
                        fence, step_id=step_id, code="INSIGHT_SUMMARY_PARTIAL_FAILURE",
                        message=f"汇总分组失败 {len(failed_units)}/{len(layer_units)}；成功分组已保存。{failed_units[0]['error']}",
                    )
                    return {"failed": True, "__already_published__": True}
                log_result(
                    f"第 {layer_index + 1} 层汇总完成｜{len(completed_indices)} 个分组",
                )
                checkpoint: dict[str, Any] = {
                    "runId": run_id, "layerIndex": layer_index,
                    "unitCount": len(completed_indices),
                }
                publish = None
            elif kind in {
                "insight_build_overview",
                "insight_stage_overview_no_spoiler",
                "insight_stage_overview_story_summary",
            }:
                template = {
                    "insight_stage_overview_no_spoiler": "no_spoiler",
                    "insight_stage_overview_story_summary": "story_summary",
                }.get(kind)
                if template is None:
                    template = _required_string(
                        config.get("template"),
                        "Insight overview template",
                    )
                if template not in OVERVIEW_TEMPLATES:
                    raise JobConflict("unsupported Insight overview template")
                summary_inputs = self.repository.summary_inputs(frozen)
                payload = self.algorithms.build_overview(
                    summary_inputs,
                    template=template,
                    config=config,
                )
                if not isinstance(payload, Mapping):
                    raise InsightConflict(
                        "overview algorithm returned a non-object result"
                    )
                _required_text(
                    payload.get("title"),
                    "overview result title",
                )
                _required_text(
                    payload.get("content"),
                    "overview result content",
                )
                log_result(
                    "作品概览生成结果",
                    json_details(payload),
                )
                checkpoint = {}

                def publish(connection: Connection) -> None:
                    checkpoint.update(
                        self.repository.publish_artifact(
                            connection=connection,
                            frozen=frozen,
                            kind="overview",
                            template=template,
                            payload=payload,
                            activate=not staging_run,
                        )
                    )
            elif kind in {
                "insight_build_compressed_context",
                "insight_stage_compressed_context",
            }:
                summary_inputs = self.repository.summary_inputs(frozen)
                payload = self.algorithms.build_compressed_context(
                    summary_inputs,
                    config=config,
                )
                if not isinstance(payload, Mapping) or not contains_nonempty_text(payload):
                    raise InsightConflict(
                        "compressed context algorithm returned an empty or non-object result"
                    )
                log_result(
                    "压缩上下文生成结果",
                    json_details(payload),
                )
                checkpoint = {}

                def publish(connection: Connection) -> None:
                    checkpoint.update(
                        self.repository.publish_artifact(
                            connection=connection,
                            frozen=frozen,
                            kind="compressed_context",
                            template="default",
                            payload=payload,
                            activate=not staging_run,
                        )
                    )
            elif kind in {
                "insight_build_entity_alignment",
                "insight_stage_entity_alignment",
            }:
                # 实体图谱独立构建：align_entities 自带独立事务并自行激活新 revision。
                # insight_stage_entity_alignment 是分析流水线里的正式步骤（timeline 之后、
                # vectors 之前），staged 产物交给所属 run 的 publish_run 统一激活；
                # insight_build_entity_alignment 仍是独立补偿作业用的入口。
                # 对齐失败不阻塞分析结果发布：run 发布后由
                # InsightAnalysisWorkerService._dispatch_entity_alignment_if_missing 补建独立作业。
                _standalone_alignment = kind == "insight_build_entity_alignment"
                checkpoint = {}
                try:
                    align_result = self.repository.align_entities(
                        book_id=frozen.book_id,
                        chapter_id="",
                        pages_analysis=frozen.pages,
                        entity_slug_map_config=self.entity_slug_map,
                        run_id=None if _standalone_alignment else run_id,
                        slug_mode=_slug_mode_from_config(config),
                        staged=False if _standalone_alignment else staging_run,
                        dependency_fingerprint=frozen.fingerprint,
                        llm_judge=self._alignment_judge(config, book_id=frozen.book_id),
                        judge_cache_path=AlignmentJudgeCache.default_path(
                            self.data_root,
                            frozen.book_id,
                        ),
                        progress=_alignment_progress,
                    )
                except Exception as exc:  # noqa: BLE001 - 对齐失败必须让分析结果继续发布
                    LOGGER.warning("实体对齐自动生成异常，继续流水线: %s", exc)
                    log_result(
                        "实体知识网络对齐失败",
                        (f"原因：{exc}", "分析结果仍会发布，稍后可重跑实体对齐"),
                    )
                else:
                    checkpoint = {
                        "artifactId": align_result.get("artifactId"),
                        "revision": align_result.get("revision"),
                        "entityCount": align_result.get("entityCount"),
                        "aliasCount": align_result.get("aliasCount"),
                        "autoMergeCount": align_result.get("autoMergeCount"),
                    }
                    log_result(
                        "实体知识网络对齐完成",
                        (
                            f"实体总数：{checkpoint['entityCount']}",
                            f"别名总数：{checkpoint['aliasCount']}",
                            f"版本：{checkpoint['revision']}",
                        ),
                    )

                def publish(connection: Connection) -> None:
                    return None

            elif kind in {
                "insight_build_timeline",
                "insight_stage_timeline",
            }:
                timeline_inputs = list(self.repository.summary_inputs(frozen))
                compressed_input = self.repository.compressed_context_input(frozen)
                if compressed_input is not None:
                    timeline_inputs.append(compressed_input)
                timeline = self.algorithms.build_timeline(
                    timeline_inputs,
                    config=config,
                )
                if not isinstance(timeline, Mapping):
                    raise InsightConflict(
                        "timeline algorithm returned a non-object result"
                    )
                log_result(
                    "剧情时间线生成结果",
                    json_details(timeline),
                )
                checkpoint = {}

                def publish(connection: Connection) -> None:
                    checkpoint.update(
                        self.repository.publish_timeline(
                            connection=connection,
                            frozen=frozen,
                            result=timeline,
                            activate=not staging_run,
                        )
                    )
            elif kind in {
                "insight_build_vectors",
                "insight_stage_vectors",
            }:
                vector_build = self._build_vectors(
                    fence=fence,
                    step=step,
                    frozen=frozen,
                    config=config,
                )
                checkpoint = {
                    key: value
                    for key, value in vector_build.items()
                    if not key.startswith("__")
                }
                def publish(connection: Connection) -> None:
                    checkpoint.update(
                        self.repository.publish_vector_generation(
                            connection=connection,
                            frozen=frozen,
                            generation=vector_build["generation"],
                            page_count=vector_build["pageCount"],
                            event_count=vector_build["eventCount"],
                            activate=not staging_run,
                        )
                    )
                self.jobs.complete_step(
                    fence,
                    step_id=step_id,
                    checkpoint=checkpoint,
                    publisher=publish,
                )
                log_result(
                    "语义索引生成完成",
                    (
                        f"页面：{checkpoint['pageCount']}/{checkpoint['pageTotal']}",
                        f"事件：{checkpoint['eventCount']}/{checkpoint['eventTotal']}",
                        f"覆盖率：{checkpoint['coverage']:.1%}",
                    ),
                )
                return {
                    **checkpoint,
                    "__already_published__": True,
                }
            else:
                raise JobConflict(f"unsupported derived step: {kind}")
            self.jobs.complete_step(
                fence,
                step_id=step_id,
                checkpoint=checkpoint,
                publisher=publish,
            )
            # 注意：实体对齐已从时间线步骤里拆出为独立步骤（insight_stage_entity_alignment），
            # 不再在这里顺带执行——否则它的耗时会被算进时间线、且用户侧看不到进展。
            return {**checkpoint, "__already_published__": True}
        except AttemptFenced:
            raise

    def _alignment_judge(self, config: Mapping[str, Any], book_id: str | None = None) -> "LLMBatchJudge | None":
        """按当前 Insight chat 配置构建实体对齐语义裁决器；解析失败回退默认裁决器。

        book_id 只用于日志与错误信息定位，不再暗示裁决器自带缓存：缓存由调用方通过
        align_entities 的 judge_cache_path 注入（AlignmentJudgeCache）。
        """
        try:
            chat_config = frozen_chat_config(config)
            return LLMBatchJudge(
                base_url=chat_config.base_url or "",
                api_key=chat_config.api_key,
                model=chat_config.model,
                provider=chat_config.provider,
                book_id=book_id,
            )
        except Exception:
            LOGGER.warning("实体对齐裁决器配置解析失败，回退默认裁决器", exc_info=True)
            return None

    def _build_vectors(
        self,
        *,
        fence: AttemptFence,
        step: Mapping[str, Any],
        frozen: AnalysisInputSnapshot,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        step_id = _required_string(step.get("stepId"), "Insight stepId")
        page_records: list[dict[str, Any]] = []
        event_records: list[dict[str, Any]] = []
        for index, page in enumerate(frozen.pages):
            analysis = _required_mapping(
                page.get("analysis"),
                f"vector page {index + 1} analysis",
            )
            summary = _required_string(
                analysis.get("page_summary"),
                f"vector page {index + 1} page_summary",
            ).strip()
            if not summary:
                raise InsightConflict(
                    f"vector page {index + 1} page_summary must not be blank"
                )
            page_id = _required_string(
                page.get("pageId"),
                f"vector page {index + 1} pageId",
            )
            page_number = _required_integer(
                page.get("pageNumber"),
                f"vector page {index + 1} pageNumber",
                minimum=1,
            )
            page_records.append(
                {
                    "id": f"page-{page_id}",
                    "document": build_page_vector_document(analysis, summary),
                    "metadata": {
                        "book_id": frozen.book_id,
                        "page_id": page_id,
                        "page_number": page_number,
                        "type": "page",
                        # 引用必须引用本页真正写的内容。document 为了召回率拼进了
                        # 角色/地点/事件，其中事件摘要描述的是「区间里某个动作」，
                        # 未必发生在本页；把原始摘要单独带出来，引用时用它。
                        "page_summary": summary,
                    },
                }
            )
        event_records.extend(self._layer_zero_event_records(frozen))
        raw_previous = step.get("checkpoint")
        if raw_previous is not None and not isinstance(raw_previous, Mapping):
            raise InsightConflict("vector checkpoint must be an object")
        resume = isinstance(raw_previous, Mapping) and bool(raw_previous)
        if resume:
            previous = _required_mapping(raw_previous, "vector checkpoint")
            if set(previous) != {
                "generation",
                "pageCount",
                "eventCount",
                "pageTotal",
                "eventTotal",
                "coverage",
            }:
                raise InsightConflict("vector checkpoint fields are invalid")
            generation = _required_integer(
                previous["generation"],
                "vector checkpoint generation",
                minimum=1,
            )
            page_count = _required_integer(
                previous["pageCount"],
                "vector checkpoint pageCount",
            )
            event_count = _required_integer(
                previous["eventCount"],
                "vector checkpoint eventCount",
            )
            page_total = _required_integer(
                previous["pageTotal"],
                "vector checkpoint pageTotal",
            )
            event_total = _required_integer(
                previous["eventTotal"],
                "vector checkpoint eventTotal",
            )
            coverage = previous["coverage"]
            if (
                isinstance(coverage, bool)
                or not isinstance(coverage, Real)
                or not math.isfinite(float(coverage))
            ):
                raise InsightConflict("vector checkpoint coverage is invalid")
            expected_total = len(page_records) + len(event_records)
            expected_coverage = (
                1.0
                if expected_total == 0
                else (page_count + event_count) / expected_total
            )
            if (
                page_total != len(page_records)
                or event_total != len(event_records)
                or not math.isclose(float(coverage), expected_coverage)
            ):
                raise InsightConflict("vector checkpoint does not match current inputs")
        else:
            generation = self.repository.next_vector_generation(frozen.book_id)
            page_count = 0
            event_count = 0
        if (
            generation < 1
            or page_count < 0
            or event_count < 0
            or page_count > len(page_records)
            or event_count > len(event_records)
        ):
            raise InsightConflict("vector checkpoint is invalid")
        checkpoint: dict[str, Any] = {
            "generation": generation,
            "pageCount": page_count,
            "eventCount": event_count,
            "pageTotal": len(page_records),
            "eventTotal": len(event_records),
        }
        total = len(page_records) + len(event_records)

        def log_progress() -> None:
            completed = checkpoint["pageCount"] + checkpoint["eventCount"]
            percent = completed * 100 // total if total else 100
            filled = percent // 5
            log_result(
                f"语义索引生成进度 [{'=' * filled}{'-' * (20 - filled)}] "
                f"{completed}/{total}（{percent}%）｜"
                f"页面 {checkpoint['pageCount']}/{checkpoint['pageTotal']}｜"
                f"事件 {checkpoint['eventCount']}/{checkpoint['eventTotal']}",
            )

        def checkpoint_batch(kind: str, count: int) -> None:
            if kind not in {"pages", "events"}:
                raise InsightConflict("vector store checkpoint kind is invalid")
            if isinstance(count, bool) or not isinstance(count, int):
                raise InsightConflict("vector store checkpoint count is invalid")
            if kind == "pages":
                if count < checkpoint["pageCount"] or count > len(page_records):
                    raise InsightConflict("vector page checkpoint is invalid")
                checkpoint["pageCount"] = count
            else:
                if count < checkpoint["eventCount"] or count > len(event_records):
                    raise InsightConflict("vector event checkpoint is invalid")
                checkpoint["eventCount"] = count
            completed = checkpoint["pageCount"] + checkpoint["eventCount"]
            checkpoint["coverage"] = 1.0 if total == 0 else completed / total

            def publish_partial(connection: Connection) -> None:
                self.repository.checkpoint_vector_generation(
                    connection=connection,
                    frozen=frozen,
                    generation=generation,
                    page_count=checkpoint["pageCount"],
                    event_count=checkpoint["eventCount"],
                )

            self.jobs.checkpoint_step(
                fence,
                step_id=step_id,
                checkpoint=checkpoint,
                publisher=publish_partial,
            )
            log_progress()

        log_progress()
        try:
            result = self.vector_store.publish_batches(
                book_id=frozen.book_id,
                generation=generation,
                page_batches=self._embedding_batches(
                    page_records[page_count:],
                    config=config,
                ),
                event_batches=self._embedding_batches(
                    event_records[event_count:],
                    config=config,
                ),
                resume=resume,
                initial_page_count=page_count,
                initial_event_count=event_count,
                expected_page_count=len(page_records),
                expected_event_count=len(event_records),
                on_batch=checkpoint_batch,
            )
        except AttemptFenced:
            raise
        except Exception:
            self.repository.fail_vector_generation(
                book_id=frozen.book_id,
                generation=generation,
            )
            raise
        try:
            if not isinstance(result, Mapping):
                raise InsightConflict("vector store returned an invalid result")
            if set(result) != {"pageCount", "eventCount"}:
                raise InsightConflict("vector store result fields are invalid")
            if (
                isinstance(result["pageCount"], bool)
                or not isinstance(result["pageCount"], int)
                or isinstance(result["eventCount"], bool)
                or not isinstance(result["eventCount"], int)
            ):
                raise InsightConflict("vector store result types are invalid")
            if (
                result["pageCount"] < page_count
                or result["pageCount"] > len(page_records)
                or result["eventCount"] < event_count
                or result["eventCount"] > len(event_records)
            ):
                raise InsightConflict("vector store result counts are invalid")
            checkpoint["pageCount"] = result["pageCount"]
            checkpoint["eventCount"] = result["eventCount"]
            if (
                result["pageCount"] != len(page_records)
                or result["eventCount"] != len(event_records)
            ):
                raise InsightConflict("vector store completed with incomplete coverage")
        except AttemptFenced:
            raise
        except Exception:
            self.repository.fail_vector_generation(
                book_id=frozen.book_id,
                generation=generation,
            )
            raise
        checkpoint["coverage"] = 1.0
        return {
            **checkpoint,
        }

    def _embedding_batches(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        config: Mapping[str, Any],
        batch_size: int = 64,
        max_chars: int = EMBEDDING_BATCH_MAX_CHARS,
    ) -> Iterable[
        tuple[Sequence[Mapping[str, Any]], Sequence[Sequence[float]]]
    ]:
        expected_dimension: int | None = None
        documents = [
            _required_string(
                row.get("document"),
                f"embedding record {index + 1} document",
            )
            for index, row in enumerate(records)
        ]
        cursor = 0
        while cursor < len(documents):
            # 同时受条数与总字数约束：上游的 input too big 是整批口径，
            # 只按 64 条切会让长文档批次整体失败。
            batch_indices: list[int] = []
            batch_chars = 0
            while cursor < len(documents) and len(batch_indices) < batch_size:
                size = len(documents[cursor])
                if batch_indices and batch_chars + size > max_chars:
                    break
                batch_indices.append(cursor)
                batch_chars += size
                cursor += 1
            batch = [records[index] for index in batch_indices]
            batch_documents = [documents[index] for index in batch_indices]
            first_number = batch_indices[0] + 1
            embeddings = list(
                self.algorithms.embed_documents(batch_documents, config=config)
            )
            if len(embeddings) != len(batch):
                raise InsightConflict("embedding result count mismatch")
            for index, embedding in enumerate(embeddings):
                if (
                    not isinstance(embedding, Sequence)
                    or isinstance(embedding, (str, bytes, bytearray))
                    or not embedding
                ):
                    raise InsightConflict(
                        f"embedding result {first_number + index} is invalid"
                    )
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, Real)
                    or not math.isfinite(float(value))
                    for value in embedding
                ):
                    raise InsightConflict(
                        f"embedding result {first_number + index} contains invalid values"
                    )
                if expected_dimension is None:
                    expected_dimension = len(embedding)
                elif len(embedding) != expected_dimension:
                    raise InsightConflict("embedding result dimensions do not match")
            yield batch, embeddings

    def _layer_zero_event_records(
        self,
        frozen: AnalysisInputSnapshot,
    ) -> list[dict[str, Any]]:
        if not frozen.source_run_id:
            return []
        with self.engine.connect() as connection:

            def query_layers(run_id: str) -> list[Mapping[str, Any]]:
                return list(
                    connection.execute(
                        select(
                            analysis_layer_results.c.id,
                            analysis_layer_results.c.content_json,
                        )
                        .where(
                            analysis_layer_results.c.run_id == run_id,
                            analysis_layer_results.c.layer_index == 0,
                            analysis_layer_results.c.status.in_(
                                ("staging", "published")
                            ),
                        )
                        .order_by(analysis_layer_results.c.unit_index)
                    ).mappings()
                )

            layers = query_layers(frozen.source_run_id)
            if not layers:
                # 局部重跑（retry-failed 复用已发布页面）产生的新 run 自身没有 layer 结果，
                # 此时回退到本书最近一次产出 layer 结果的 run；否则向量索引会静默按 0 条事件发布。
                fallback_run_ids = [
                    _required_string(row["run_id"], "fallback layer run id")
                    for row in connection.execute(
                        select(analysis_layer_results.c.run_id)
                        .join(
                            analysis_runs,
                            analysis_runs.c.id
                            == analysis_layer_results.c.run_id,
                        )
                        .where(
                            analysis_runs.c.book_id == frozen.book_id,
                            analysis_layer_results.c.layer_index == 0,
                            analysis_layer_results.c.status.in_(
                                ("staging", "published")
                            ),
                            analysis_layer_results.c.run_id
                            != frozen.source_run_id,
                        )
                        .group_by(analysis_layer_results.c.run_id)
                        .order_by(
                            func.max(analysis_layer_results.c.created_at).desc()
                        )
                    ).mappings()
                ]
                for fallback_run_id in fallback_run_ids:
                    layers = query_layers(fallback_run_id)
                    if layers:
                        break
            page_rows = list(
                connection.execute(
                    select(
                        analysis_layer_result_pages.c.layer_result_id,
                        analysis_layer_result_pages.c.page_id_snapshot,
                        analysis_layer_result_pages.c.page_number_snapshot,
                    )
                    .where(
                        analysis_layer_result_pages.c.layer_result_id.in_(
                            tuple(
                                _required_string(
                                    row["id"],
                                    "stored analysis layer id",
                                )
                                for row in layers
                            )
                        )
                    )
                    .order_by(
                        analysis_layer_result_pages.c.layer_result_id,
                        analysis_layer_result_pages.c.ordinal,
                    )
                )
            ) if layers else []
        pages_by_layer: dict[str, list[tuple[str, int]]] = {}
        for layer_result_id, page_id, page_number in page_rows:
            pages_by_layer.setdefault(
                _required_string(
                    layer_result_id,
                    "stored analysis layer page layer id",
                ),
                [],
            ).append(
                (
                    _required_string(
                        page_id,
                        "stored analysis layer page id",
                    ),
                    _required_integer(
                        page_number,
                        "stored analysis layer page number",
                        minimum=1,
                    ),
                )
            )
        records: list[dict[str, Any]] = []
        for layer in layers:
            layer_id = _required_string(
                layer["id"],
                "stored analysis layer id",
            )
            content = _json_object(
                layer["content_json"],
                "analysis layer content",
            )
            page_refs = pages_by_layer.get(layer_id, [])
            if not page_refs:
                raise InsightPrecondition(
                    "analysis layer has no covered pages"
                )
            try:
                events = _normalize_layer_events(content.get("key_events", []))
            except ValueError as exc:
                raise InsightConflict(str(exc)) from exc
            for index, event in enumerate(events, start=1):
                records.append(
                    {
                        "id": f"event-{layer_id}-{index}",
                        "document": event["summary"],
                        "metadata": {
                            "book_id": frozen.book_id,
                            "page_id": page_refs[0][0],
                            "page_number": page_refs[0][1],
                            "page_ids_json": _json(
                                [value[0] for value in page_refs]
                            ),
                            "page_numbers_json": _json(
                                [value[1] for value in page_refs]
                            ),
                            "importance": event["importance"],
                            "type": "event",
                        },
                    }
                )
        return records

    def _with_credentials(
        self,
        config: Mapping[str, Any],
        *,
        section_names: Sequence[str],
    ) -> dict[str, Any]:
        try:
            return self.credentials.resolve_credential_sections(
                config,
                section_names,
            )
        except LookupError as exc:
            raise JobConflict(
                "frozen Insight credential version no longer exists"
            ) from exc


def _publication_status(
    frozen: AnalysisInputSnapshot,
    current: AnalysisInputSnapshot,
) -> str:
    if frozen.fingerprint != current.fingerprint:
        return "stale"
    if "completed_with_errors" in {frozen.source_run_status, current.source_run_status}:
        return "degraded"
    return "ready"


def _analysis_input_fingerprint(
    pages_payload: Sequence[Mapping[str, Any]],
) -> str:
    """Hash the same immutable identity fields before and after publication."""

    canonical: list[dict[str, object]] = []
    for index, page in enumerate(pages_payload, start=1):
        canonical.append(
            {
                "resultId": _required_string(
                    page.get("resultId"),
                    f"analysis input {index} resultId",
                ),
                "pageId": _required_string(
                    page.get("pageId"),
                    f"analysis input {index} pageId",
                ),
                "pageNumber": _required_integer(
                    page.get("pageNumber"),
                    f"analysis input {index} pageNumber",
                    minimum=1,
                ),
                "sourceChecksum": _required_sha256(
                    page.get("sourceChecksum"),
                    f"analysis input {index} sourceChecksum",
                ),
                "currentSourceChecksum": _required_sha256(
                    page.get("currentSourceChecksum"),
                    f"analysis input {index} currentSourceChecksum",
                ),
            }
        )
    return hashlib.sha256(_json(canonical).encode("utf-8")).hexdigest()


def _layer_prompt_type(
    *,
    layer_index: int,
    layer_count: int,
    align_to_chapter: bool,
) -> str:
    if layer_index == 0:
        return "batch_analysis"
    if layer_index == layer_count - 1:
        return "book_overview"
    if align_to_chapter:
        return "chapter_summary"
    return "segment_summary"


def _page_context(pages: Sequence[Mapping[str, Any]]) -> str:
    contexts = []
    for index, page in enumerate(pages, start=1):
        analysis = _required_mapping(
            page.get("analysis"),
            f"Insight context input {index} analysis",
        )
        if not analysis:
            raise InsightConflict(
                f"Insight context input {index} analysis must not be empty"
            )
        contexts.append(_page_context_label(page) + "\n" + _json(analysis))
    if not contexts:
        raise InsightConflict("Insight context inputs must not be empty")
    return "\n\n".join(contexts)


def _page_context_label(page: Mapping[str, Any]) -> str:
    raw_page_ids = page.get("pageIds")
    raw_page_numbers = page.get("pageNumbers")
    if raw_page_ids is None:
        page_ids = [_required_string(page.get("pageId"), "Insight context pageId")]
    else:
        if not isinstance(raw_page_ids, list) or not raw_page_ids:
            raise InsightConflict("Insight context pageIds must be a non-empty array")
        page_ids = [
            _required_string(value, "Insight context pageId")
            for value in raw_page_ids
        ]
    if raw_page_numbers is None:
        page_numbers = [
            _required_integer(
                page.get("pageNumber"),
                "Insight context pageNumber",
                minimum=1,
            )
        ]
    else:
        if not isinstance(raw_page_numbers, list) or not raw_page_numbers:
            raise InsightConflict(
                "Insight context pageNumbers must be a non-empty array"
            )
        page_numbers = [
            _required_integer(
                value,
                "Insight context pageNumber",
                minimum=1,
            )
            for value in raw_page_numbers
        ]
    if len(page_ids) != len(page_numbers):
        raise InsightConflict("Insight context page references do not match")
    page_range = (
        str(page_numbers[0])
        if len(page_numbers) == 1
        else f"{page_numbers[0]}-{page_numbers[-1]}"
    )
    return f"第 {page_range} 页（page_ids={_json(page_ids)}，page_numbers={_json(page_numbers)}）"

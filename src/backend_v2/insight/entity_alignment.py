"""实体对齐的图组织与批量语义裁决。

设计要点（与逐对裁决的根本差异）：

1. **连通分量只用来组织输入**。候选边构成的无向图按连通分量切分，分量只是"这些实体
   之间存在候选关系，可以放在一起考察"的工作单元，绝不等于最终实体簇。
2. **裁决输出三态**：SAME / DIFFERENT / UNCERTAIN。只有 SAME 才能触发合并；
   未裁决的候选边默认 UNCERTAIN，既不合并也不推断为不同。
3. **冲突保护**：已明确 DIFFERENT 的实体对不允许被另一条路径传递合并。合并使用
   带约束的并查集（`ConflictSafeClusters`），冲突时阻止合并并把实体保留为独立。
4. **超预算分量分片**：分片只改变计算方式，不改变语义——同一分量跨片共享实体 ID，
   跨片结果统一汇总去重后再做冲突校验。
5. **可追溯**：原始实体、别名、页面出处、候选边、裁决状态全部保留，最终只写
   `canonical_id` 映射，不物理删除任何原始实体。

本模块不 import `derived`，所有项目内依赖以可调用对象注入，避免循环导入。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

LOGGER = logging.getLogger("saber.worker.entity_alignment")

SAME = "SAME"
DIFFERENT = "DIFFERENT"
UNCERTAIN = "UNCERTAIN"

# 延迟原因（低频概念 / 通用词）
DEFERRED_LOW_FREQUENCY = "deferred_low_frequency"
DEFERRED_GENERIC_TERM = "deferred_generic_term"

ALIGNMENT_CATEGORIES = ("characters", "locations", "abilities", "items", "concepts")

# 内置通用词表（可通过配置追加/覆盖，不硬编码进判定逻辑）。
DEFAULT_GENERIC_LOCATION_TERMS = frozenset({
    "室内", "室外", "房间", "屋子", "房间内", "客厅", "卧室", "厨房", "浴室", "厕所",
    "走廊", "楼道", "楼梯", "门口", "窗边", "床上", "地面", "天花板", "大楼", "公寓",
    "家", "家里", "学校", "教室", "办公室", "医院", "病房", "公园", "街道", "马路",
    "车站", "站台", "商店", "店铺", "超市", "餐厅", "咖啡馆", "酒吧", "森林", "树林",
    "海", "海边", "河", "河边", "山", "山上", "天空", "城市", "小镇", "村庄", "空地",
    "停车场", "天台", "楼顶", "地下室", "仓库", "工地", "广场", "会场", "体育馆",
})

DEFAULT_GENERIC_ITEM_TERMS = frozenset({
    "手机", "电话", "电脑", "笔记本", "相机", "电视", "书", "书本", "报纸", "杂志",
    "笔", "纸", "桌子", "椅子", "床", "杯子", "瓶子", "餐具", "食物", "饮料", "酒",
    "咖啡", "茶", "水", "钱", "现金", "纸币", "硬币", "钱包", "银行卡", "信用卡",
    "包", "背包", "手提包", "衣服", "外套", "鞋", "帽子", "眼镜", "钥匙", "信封",
    "文件", "合同", "照片", "刀", "剑", "枪", "武器", "车", "汽车", "自行车",
    "摩托车", "电车", "地铁", "飞机", "船",
})


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class AlignmentConfig:
    """对齐流程的集中配置（环境变量可覆盖，命名沿用项目既有 INSIGHT_* 风格）。"""

    concept_min_pages: int = 3
    top_k_per_node: int = 4
    max_nodes_per_request: int = 24
    max_edges_per_request: int = 40
    max_input_tokens: int = 8000
    max_output_tokens: int = 2500
    chapter_window: int = 1
    max_attempts_per_request: int = 3
    parallel: int = 3
    generic_terms_path: str = ""


def resolve_alignment_config(**overrides: Any) -> AlignmentConfig:
    """按「显式参数 > 环境变量 > 默认值」解析配置。"""
    base = {
        "concept_min_pages": _env_int("INSIGHT_ALIGN_CONCEPT_MIN_PAGES", 3, minimum=1, maximum=50),
        "top_k_per_node": _env_int("INSIGHT_ALIGN_TOP_K", 4, minimum=1, maximum=40),
        "max_nodes_per_request": _env_int("INSIGHT_ALIGN_MAX_NODES", 24, minimum=2, maximum=400),
        "max_edges_per_request": _env_int("INSIGHT_ALIGN_MAX_EDGES", 40, minimum=1, maximum=2000),
        "max_input_tokens": _env_int("INSIGHT_ALIGN_INPUT_TOKENS", 8000, minimum=1000, maximum=200000),
        "max_output_tokens": _env_int("INSIGHT_ALIGN_OUTPUT_TOKENS", 2500, minimum=256, maximum=64000),
        "chapter_window": _env_int("INSIGHT_ALIGN_CHAPTER_WINDOW", 1, minimum=0, maximum=100),
        "max_attempts_per_request": _env_int("INSIGHT_ALIGN_ATTEMPTS", 3, minimum=1, maximum=6),
        "parallel": _env_int("INSIGHT_ALIGN_PARALLEL", 3, minimum=1, maximum=8),
        "generic_terms_path": os.environ.get("INSIGHT_ALIGN_GENERIC_TERMS", "").strip(),
    }
    base.update({key: value for key, value in overrides.items() if value is not None})
    return AlignmentConfig(**base)


_GENERIC_CACHE: dict[tuple[str, str], frozenset[str]] = {}


def _generic_terms(category: str, path: str) -> frozenset[str]:
    key = (category, path or "")
    cached = _GENERIC_CACHE.get(key)
    if cached is not None:
        return cached
    defaults = {
        "locations": DEFAULT_GENERIC_LOCATION_TERMS,
        "items": DEFAULT_GENERIC_ITEM_TERMS,
    }.get(category, frozenset())
    merged: set[str] = set(defaults)
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            extra = loaded.get(category) if isinstance(loaded, Mapping) else None
            if isinstance(extra, Sequence) and not isinstance(extra, (str, bytes)):
                merged.update(str(item).strip() for item in extra if str(item).strip())
        except Exception:  # noqa: BLE001 - 词表缺失不应阻断对齐
            # 词表文件只是「额外补充」，读不出来就退回内置默认词表；
            # 但要留下痕迹，否则用户改了词表却不生效时无从查起。
            LOGGER.warning("通用词表读取失败，改用内置默认词表：%s", path, exc_info=True)
    frozen = frozenset(merged)
    _GENERIC_CACHE[key] = frozen
    return frozen


def generic_terms_for(category: str, path: str = "") -> frozenset[str]:
    """暴露给测试/调用方的通用词表读取入口。"""
    return _generic_terms(category, path)


# ---------------------------------------------------------------------------
# 名称标准化
# ---------------------------------------------------------------------------

_BRACKET_RE = re.compile(r"[\(（\[【].*?[\)）\]】]")


def normalize_entity_name(name: str) -> str:
    """标准化名称：全半角、空白、括号补充说明、首尾标点。"""
    text = unicodedata.normalize("NFKC", str(name or "")).strip()
    text = _BRACKET_RE.sub("", text)
    text = re.sub(r"\s+", "", text)
    text = text.strip("·-—_.,，。:：;；\"'“”‘’")
    return text.casefold()


def normalize_generic_terms(terms: Iterable[str]) -> frozenset[str]:
    return frozenset(normalize_entity_name(term) for term in terms if normalize_entity_name(term))


def is_bare_generic(name: str, normalized_terms: frozenset[str]) -> bool:
    """是否「无修饰的通用词」：标准化后直接命中通用词表。"""
    return normalize_entity_name(name) in normalized_terms


def _char_overlap(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _similar_enough(a: str, b: str) -> bool:
    """通用词之间的"同词"判定：一方是另一方子串，或字符重合度 >= 0.9。"""
    na, nb = normalize_entity_name(a), normalize_entity_name(b)
    if not na or not nb:
        return False
    return na in nb or nb in na or _char_overlap(na, nb) >= 0.9


# ---------------------------------------------------------------------------
# 图结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityNode:
    node_id: str
    category: str
    name: str
    distinct_pages: int
    pages: tuple[int, ...] = ()
    existing_entity_id: str | None = None

    @property
    def is_existing(self) -> bool:
        return bool(self.existing_entity_id)


@dataclass(frozen=True)
class CandidateEdge:
    pair_id: str
    category: str
    name_a: str
    name_b: str
    score: float
    note: str = ""


@dataclass(frozen=True)
class Component:
    component_id: str
    category: str
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.node_ids)


@dataclass
class GraphBuildResult:
    nodes: dict[str, EntityNode]
    edges: dict[str, CandidateEdge]
    components: list[Component]
    orphans: list[str] = field(default_factory=list)
    deferred: list[dict[str, Any]] = field(default_factory=list)

    def node_name(self, node_id: str) -> str:
        node = self.nodes.get(node_id)
        return node.name if node else node_id

    def name_to_node(self) -> dict[str, str]:
        return {node.name: node_id for node_id, node in self.nodes.items()}


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        root_l, root_r = self.find(left), self.find(right)
        if root_l != root_r:
            # 稳定合并方向：字典序小的当根，保证同样输入产生同样分量编号
            if root_r < root_l:
                root_l, root_r = root_r, root_l
            self.parent[root_r] = root_l


def edge_pair_id(category: str, name_a: str, name_b: str) -> str:
    first, second = sorted((name_a, name_b))
    digest = hashlib.sha1(f"{category}::{first}::{second}".encode("utf-8")).hexdigest()[:12]
    return f"{category[:4]}_{digest}"


def build_alignment_graph(
    *,
    category: str,
    nodes: Iterable[EntityNode],
    candidate_filter: Callable[[str, str, str], bool],
    config: AlignmentConfig,
    known_decisions: Mapping[str, str] | None = None,
) -> GraphBuildResult:
    """按类别构建候选图：返回节点、边、连通分量、孤立节点与延迟项。

    - `candidate_filter(category, name_a, name_b)` 由调用方注入（项目既有的粗筛闸门），
      本模块不重复实现语义规则；
    - 每个节点最多保留 `top_k_per_node` 条边（按字符重合度排序），避免 O(n²) 爆炸；
    - concepts 低频词条（distinct_pages < concept_min_pages）不进入模糊匹配队列，
      但完整的原始实体信息由调用方保留；
    - 通用地点/物品词之间若都无修饰，则低价值匹配不进入 LLM 队列。
    """
    node_map = {node.node_id: node for node in nodes}
    decisions = dict(known_decisions or {})
    deferred: list[dict[str, Any]] = []
    generic = _generic_terms(category, config.generic_terms_path) if category in ("locations", "items") else frozenset()
    generic_norm = normalize_generic_terms(generic)

    active: list[EntityNode] = []
    for node in node_map.values():
        if category == "concepts" and not node.is_existing and node.distinct_pages < config.concept_min_pages:
            deferred.append({
                "node_id": node.node_id,
                "name": node.name,
                "category": category,
                "distinct_pages": node.distinct_pages,
                "pages": list(node.pages),
                "reason": DEFERRED_LOW_FREQUENCY,
            })
            continue
        active.append(node)

    # 候选边：每个节点只与最相似的 top_k 邻居配对
    candidates: dict[tuple[str, str], tuple[float, str]] = {}
    for index, node in enumerate(active):
        node_norm = normalize_entity_name(node.name)
        scored: list[tuple[float, str]] = []
        for other in active[index + 1:]:
            if other.name == node.name:
                continue
            if generic_norm and is_bare_generic(node.name, generic_norm) and is_bare_generic(other.name, generic_norm):
                if not _similar_enough(node.name, other.name):
                    deferred.append({
                        "node_id": node.node_id,
                        "peer_node_id": other.node_id,
                        "name": node.name,
                        "peer_name": other.name,
                        "category": category,
                        "reason": DEFERRED_GENERIC_TERM,
                    })
                    continue
            if not candidate_filter(category, node.name, other.name):
                continue
            score = _char_overlap(node_norm, normalize_entity_name(other.name))
            scored.append((score, other.name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        for score, other_name in scored[: config.top_k_per_node]:
            key = tuple(sorted((node.name, other_name)))
            if key not in candidates:
                candidates[key] = (score, f"char_overlap={score:.2f}")

    node_id_by_name = {node.name: node.node_id for node in active}
    edges: dict[str, CandidateEdge] = {}
    for (name_a, name_b), (score, note) in sorted(candidates.items()):
        if name_a not in node_id_by_name or name_b not in node_id_by_name:
            continue
        pair_id = edge_pair_id(category, name_a, name_b)
        prior = decisions.get(f"{category}::{name_a}::{name_b}") or decisions.get(f"{category}::{name_b}::{name_a}")
        edges[pair_id] = CandidateEdge(
            pair_id=pair_id,
            category=category,
            name_a=name_a,
            name_b=name_b,
            score=score,
            note=f"{note};prior={prior}" if prior else note,
        )

    # 连通分量
    union = _UnionFind()
    for edge in edges.values():
        union.union(node_id_by_name[edge.name_a], node_id_by_name[edge.name_b])
    grouped: dict[str, dict[str, list[str]]] = {}
    for edge in edges.values():
        root = union.find(node_id_by_name[edge.name_a])
        bucket = grouped.setdefault(root, {"nodes": [], "edges": []})
        bucket["edges"].append(edge.pair_id)
        for name in (edge.name_a, edge.name_b):
            node_id = node_id_by_name[name]
            if node_id not in bucket["nodes"]:
                bucket["nodes"].append(node_id)

    components: list[Component] = []
    for index, root in enumerate(sorted(grouped)):
        bucket = grouped[root]
        components.append(Component(
            component_id=f"{category}_{index:04d}",
            category=category,
            node_ids=tuple(sorted(bucket["nodes"])),
            edge_ids=tuple(sorted(bucket["edges"])),
        ))

    connected = {node_id for component in components for node_id in component.node_ids}
    orphans = sorted(node.node_id for node in active if node.node_id not in connected)
    return GraphBuildResult(nodes=node_map, edges=edges, components=components, orphans=orphans, deferred=deferred)


# ---------------------------------------------------------------------------
# 请求打包
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestShard:
    request_id: str
    components: tuple[Component, ...]
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
    split_component_ids: tuple[str, ...] = ()
    cross_shard_node_ids: tuple[str, ...] = ()

    @property
    def edge_count(self) -> int:
        return len(self.edge_ids)

    @property
    def node_count(self) -> int:
        return len(self.node_ids)


def _estimate_tokens(payload: Any) -> int:
    """保守估算：CJK 约 1.5 字符/token，加上 JSON 结构开销。"""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return int(len(text) / 1.5) + 64


def _split_component(
    component: Component,
    graph: GraphBuildResult,
    config: AlignmentConfig,
) -> list[Component]:
    """把超预算分量按边序切成若干分片；分片沿用原 component_id。"""
    name_to_node = graph.name_to_node()

    shards: list[Component] = []
    current_nodes: list[str] = []
    current_edges: list[str] = []
    for edge_id in component.edge_ids:
        edge = graph.edges[edge_id]
        node_ids = [name_to_node[edge.name_a], name_to_node[edge.name_b]]
        too_many_nodes = len(set(current_nodes) | set(node_ids)) > config.max_nodes_per_request
        too_many_edges = len(current_edges) >= config.max_edges_per_request
        if (too_many_nodes or too_many_edges) and current_edges:
            shards.append(Component(
                component_id=component.component_id,
                category=component.category,
                node_ids=tuple(sorted(current_nodes)),
                edge_ids=tuple(sorted(current_edges)),
            ))
            current_nodes, current_edges = [], []
        for node_id in node_ids:
            if node_id not in current_nodes:
                current_nodes.append(node_id)
        current_edges.append(edge_id)
    if current_edges:
        shards.append(Component(
            component_id=component.component_id,
            category=component.category,
            node_ids=tuple(sorted(current_nodes)),
            edge_ids=tuple(sorted(current_edges)),
        ))
    return shards


def plan_requests(*, graph: GraphBuildResult, config: AlignmentConfig) -> list[RequestShard]:
    """把连通分量打包成 LLM 请求：小分量合批、大分量独立分片。"""
    pending: list[Component] = []
    split_ids: set[str] = set()
    for component in graph.components:
        if component.size > config.max_nodes_per_request or len(component.edge_ids) > config.max_edges_per_request:
            split_ids.add(component.component_id)
            pending.extend(_split_component(component, graph, config))
        else:
            pending.append(component)

    pending.sort(key=lambda item: (item.size, item.component_id))
    requests: list[RequestShard] = []
    current: list[Component] = []
    node_ids: list[str] = []
    edge_ids: list[str] = []

    def flush() -> None:
        nonlocal current, node_ids, edge_ids
        if not current:
            return
        requests.append(RequestShard(
            request_id=f"req_{len(requests) + 1:04d}",
            components=tuple(current),
            node_ids=tuple(sorted(set(node_ids))),
            edge_ids=tuple(sorted(set(edge_ids))),
            split_component_ids=tuple(sorted({c.component_id for c in current if c.component_id in split_ids})),
        ))
        current, node_ids, edge_ids = [], [], []

    for component in pending:
        merged_nodes = sorted(set(node_ids) | set(component.node_ids))
        merged_edges = sorted(set(edge_ids) | set(component.edge_ids))
        if current and (
            len(merged_nodes) > config.max_nodes_per_request
            or len(merged_edges) > config.max_edges_per_request
        ):
            flush()
            merged_nodes = sorted(set(component.node_ids))
            merged_edges = sorted(set(component.edge_ids))
        current.append(component)
        node_ids = merged_nodes
        edge_ids = merged_edges
        if _estimate_tokens(_request_payload(graph, current, node_ids, edge_ids)) > config.max_input_tokens:
            current.pop()
            node_ids = sorted({nid for comp in current for nid in comp.node_ids})
            edge_ids = sorted({eid for comp in current for eid in comp.edge_ids})
            flush()
            current = [component]
            node_ids = sorted(set(component.node_ids))
            edge_ids = sorted(set(component.edge_ids))
    flush()

    # 跨片实体：同一分量被拆分时重复出现的实体
    occurrences: dict[str, set[str]] = {}
    for shard in requests:
        for component_id in shard.split_component_ids:
            for node_id in shard.node_ids:
                node_category = graph.nodes[node_id].category if node_id in graph.nodes else ""
                if node_category != _component_category(graph, component_id):
                    continue
                occurrences.setdefault(f"{component_id}::{node_id}", set()).add(shard.request_id)
    if occurrences:
        per_shard: dict[str, list[str]] = {}
        for key, request_ids in occurrences.items():
            if len(request_ids) < 2:
                continue
            node_id = key.split("::", 1)[1]
            for request_id in request_ids:
                per_shard.setdefault(request_id, []).append(node_id)
        requests = [
            RequestShard(
                request_id=shard.request_id,
                components=shard.components,
                node_ids=shard.node_ids,
                edge_ids=shard.edge_ids,
                split_component_ids=shard.split_component_ids,
                cross_shard_node_ids=tuple(sorted(per_shard.get(shard.request_id, []))),
            )
            for shard in requests
        ]
    return requests


def _component_category(graph: GraphBuildResult, component_id: str) -> str:
    for component in graph.components:
        if component.component_id == component_id:
            return component.category
    return ""


def _node_payload(node: EntityNode) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": node.name,
        "category": node.category,
        "pages": node.distinct_pages,
    }
    if node.existing_entity_id:
        payload["canonical_id"] = node.existing_entity_id
    if node.pages:
        payload["page_range"] = [min(node.pages), max(node.pages)]
    return payload


def _request_payload(
    graph: GraphBuildResult,
    components: Sequence[Component],
    node_ids: Sequence[str],
    edge_ids: Sequence[str],
) -> dict[str, Any]:
    component_payload = []
    for component in components:
        component_payload.append({
            "component_id": component.component_id,
            "entity_ids": sorted({graph.node_name(nid) for nid in component.node_ids}),
            "candidate_pairs": [
                {
                    "pair_id": edge_id,
                    "a": graph.edges[edge_id].name_a,
                    "b": graph.edges[edge_id].name_b,
                }
                for edge_id in component.edge_ids
            ],
        })
    return {
        "entities": [_node_payload(graph.nodes[nid]) for nid in node_ids],
        "components": component_payload,
    }


ALIGNMENT_SYSTEM_PROMPT = "你是漫画知识库实体对齐裁决专家。只输出合法 JSON，不要输出任何解释性文字。"


def build_request_prompt(graph: GraphBuildResult, shard: RequestShard) -> str:
    payload = _request_payload(graph, shard.components, shard.node_ids, shard.edge_ids)
    return f"""请对下面的候选实体组做「同一实体」裁决。

## 输入
{json.dumps(payload, ensure_ascii=False, indent=1)}

## 裁决规则
1. 只把证据充分的实体放进 `groups`（SAME）；同组必须是同一核心实体。仅凭名称相似、共享常见字、同属一个类别，都不构成同一实体。
2. 不同译名/简称/繁简异构/OCR 变体，且有别名或上下文印证 → SAME。
3. 不同分支招式、不同具体地点、不同数值或日期、亲属与从属称谓（X 与 X母亲、X 与 X的室友）、共享姓氏但名字不同的角色、物品核心概念不同 → DIFFERENT。
4. 证据不足、无法判断 → 归入 `uncertain_pair_ids`，不要猜。
5. `canonical_entity_id` 必须取自该 component 的 `entity_ids`，且必须属于同组。
6. 没有进入任何 SAME 组的实体，放进 `independent_entity_ids`；这**不表示**它与别人不同。
7. 每条候选边 `pair_id` 必须且只能出现在 groups / different_pair_ids / uncertain_pair_ids 之一。
8. 不得把不同分量混成一个组：每组的实体必须来自同一个 component。

## 输出格式
{{
  "components": [
    {{
      "component_id": "组件 id",
      "groups": [{{"canonical_entity_id": "实体名", "member_entity_ids": ["实体名"], "reason_code": "ALIAS_MATCH"}}],
      "different_pair_ids": ["pair_id"],
      "uncertain_pair_ids": ["pair_id"],
      "independent_entity_ids": ["实体名"],
      "evidence": [{{"pair_id": "pair_id", "decision": "SAME", "reason_code": "SHARED_ALIAS", "summary": "简短依据"}}]
    }}
  ]
}}"""


# ---------------------------------------------------------------------------
# 响应校验
# ---------------------------------------------------------------------------


class AlignmentProtocolError(ValueError):
    """模型输出不满足对齐协议（缺 ID、重复分配、越界等）。"""


@dataclass
class ComponentDecision:
    component_id: str
    groups: list[dict[str, Any]] = field(default_factory=list)
    different_pair_ids: list[str] = field(default_factory=list)
    uncertain_pair_ids: list[str] = field(default_factory=list)
    independent_entity_ids: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RequestDecision:
    components: dict[str, ComponentDecision] = field(default_factory=dict)
    pair_states: dict[str, str] = field(default_factory=dict)


def _edge_components(shard: RequestShard) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for component in shard.components:
        for edge_id in component.edge_ids:
            mapping[edge_id] = component.component_id
    return mapping


def validate_request_decision(payload: Any, shard: RequestShard, graph: GraphBuildResult) -> RequestDecision:
    """校验模型输出：ID 越界、重复分配、分组冲突、覆盖不全都视为协议失败。"""
    if not isinstance(payload, Mapping):
        raise AlignmentProtocolError("响应必须是 JSON 对象")
    raw_components = payload.get("components")
    if not isinstance(raw_components, list):
        raise AlignmentProtocolError("响应缺少 components 数组")

    known_components = {component.component_id for component in shard.components}
    edge_owner = _edge_components(shard)
    known_edges = set(shard.edge_ids)

    decision = RequestDecision()
    assigned_edges: dict[str, str] = {}
    assigned_entities: dict[str, str] = {}
    reported_components: set[str] = set()

    for raw in raw_components:
        if not isinstance(raw, Mapping):
            raise AlignmentProtocolError("components 元素必须是对象")
        component_id = str(raw.get("component_id") or "").strip()
        if component_id not in known_components:
            raise AlignmentProtocolError(f"未知 component_id: {component_id}")
        if component_id in reported_components:
            raise AlignmentProtocolError(f"component_id 重复出现: {component_id}")
        reported_components.add(component_id)
        component = ComponentDecision(component_id=component_id)
        component_entities = {
            graph.node_name(nid)
            for comp in shard.components
            if comp.component_id == component_id
            for nid in comp.node_ids
        }

        for group in raw.get("groups") or []:
            if not isinstance(group, Mapping):
                raise AlignmentProtocolError("groups 元素必须是对象")
            canonical = str(group.get("canonical_entity_id") or "").strip()
            members = [str(item).strip() for item in (group.get("member_entity_ids") or []) if str(item).strip()]
            if not canonical or canonical not in component_entities:
                raise AlignmentProtocolError(f"canonical_entity_id 不在输入范围: {canonical}")
            if canonical not in members:
                members.append(canonical)
            for member in members:
                if member not in component_entities:
                    raise AlignmentProtocolError(f"成员实体不在输入范围: {member}")
                owner = assigned_entities.get(member)
                if owner is not None and owner != canonical:
                    raise AlignmentProtocolError(f"实体被分配到多个冲突分组: {member}")
                assigned_entities[member] = canonical
            component.groups.append({
                "canonical_entity_id": canonical,
                "member_entity_ids": sorted(set(members)),
                "reason_code": str(group.get("reason_code") or "LLM_SAME"),
            })

        for field_name, state in (("different_pair_ids", DIFFERENT), ("uncertain_pair_ids", UNCERTAIN)):
            for pair_id in raw.get(field_name) or []:
                pair_id = str(pair_id).strip()
                if pair_id not in known_edges:
                    raise AlignmentProtocolError(f"未知 pair_id: {pair_id}")
                if edge_owner.get(pair_id) != component_id:
                    raise AlignmentProtocolError(f"pair_id 属于其它分量: {pair_id}")
                previous = assigned_edges.get(pair_id)
                if previous is not None and previous != state:
                    raise AlignmentProtocolError(f"pair_id 被重复分配为冲突状态: {pair_id}")
                assigned_edges[pair_id] = state
                target = component.different_pair_ids if state == DIFFERENT else component.uncertain_pair_ids
                if pair_id not in target:
                    target.append(pair_id)

        for entity in raw.get("independent_entity_ids") or []:
            entity = str(entity).strip()
            if entity and entity not in component_entities:
                raise AlignmentProtocolError(f"independent 实体不在输入范围: {entity}")
            if entity:
                component.independent_entity_ids.append(entity)

        for item in raw.get("evidence") or []:
            if not isinstance(item, Mapping):
                continue
            component.evidence.append({
                "pair_id": str(item.get("pair_id") or ""),
                "decision": str(item.get("decision") or ""),
                "reason_code": str(item.get("reason_code") or ""),
                "summary": str(item.get("summary") or "")[:200],
            })

        decision.components[component_id] = component

    # 覆盖性：分组内的成员对视为 SAME，与 DIFFERENT/UNCERTAIN 必须互斥且覆盖全部候选边
    same_from_groups: dict[str, str] = {}
    for component_id, component in decision.components.items():
        component_edges = [edge_id for edge_id in shard.edge_ids if edge_owner.get(edge_id) == component_id]
        for group in component.groups:
            members = set(group["member_entity_ids"])
            for edge_id in component_edges:
                edge = graph.edges[edge_id]
                if edge.name_a in members and edge.name_b in members:
                    same_from_groups[edge_id] = component_id

    for edge_id, component_id in same_from_groups.items():
        state = assigned_edges.get(edge_id)
        if state == DIFFERENT:
            raise AlignmentProtocolError(f"pair_id 同时被判为 SAME 与 DIFFERENT: {edge_id}")
        assigned_edges[edge_id] = SAME
        owner_component = decision.components.get(component_id)
        if owner_component is not None and edge_id in owner_component.uncertain_pair_ids:
            owner_component.uncertain_pair_ids.remove(edge_id)

    missing = [edge_id for edge_id in shard.edge_ids if edge_id not in assigned_edges]
    if missing:
        raise AlignmentProtocolError(f"候选边未全部裁决: {len(missing)} 条")

    decision.pair_states = assigned_edges
    return decision


# ---------------------------------------------------------------------------
# 合并与冲突保护
# ---------------------------------------------------------------------------


class ConflictSafeClusters:
    """带 DIFFERENT 约束的并查集：合并会制造矛盾时阻止合并并记录冲突。

    键为 `类别::实体名`，避免不同类别的同名实体（如地点"东京"与概念"东京"）互相污染。
    """

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._members: dict[str, set[str]] = {}
        self.different: set[tuple[str, str]] = set()
        self.blocked: list[dict[str, Any]] = []

    def add(self, key: str) -> None:
        if key not in self._parent:
            self._parent[key] = key
            self._members[key] = {key}

    def find(self, key: str) -> str:
        self.add(key)
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:
            self._parent[key], key = root, self._parent[key]
        return root

    def mark_different(self, left: str, right: str) -> None:
        if not left or not right or left == right:
            return
        self.different.add(tuple(sorted((left, right))))

    def _conflict(self, left_root: str, right_root: str) -> tuple[str, str] | None:
        members_right = self._members.get(right_root, {right_root})
        for member in self._members.get(left_root, {left_root}):
            for other in members_right:
                if tuple(sorted((member, other))) in self.different:
                    return member, other
        return None

    def union(self, left: str, right: str, *, reason: str = "") -> bool:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return True
        conflict = self._conflict(left_root, right_root)
        if conflict is not None:
            self.blocked.append({
                "left": left.split("::", 1)[-1],
                "right": right.split("::", 1)[-1],
                "conflict_pair": [item.split("::", 1)[-1] for item in conflict],
                "reason": reason or "different_constraint",
            })
            return False
        if right_root < left_root:
            left_root, right_root = right_root, left_root
        self._parent[right_root] = left_root
        self._members[left_root] |= self._members.pop(right_root, set())
        return True

    def cluster_members(self) -> dict[str, frozenset[str]]:
        clusters: dict[str, set[str]] = {}
        for key in list(self._parent):
            clusters.setdefault(self.find(key), set()).add(key)
        return {root: frozenset(members) for root, members in clusters.items()}

    def cluster_of(self, key: str) -> str:
        return self.find(key)


@dataclass
class AlignmentOutcome:
    clusters: ConflictSafeClusters
    pair_states: dict[str, str]
    deferred: list[dict[str, Any]]
    metrics: dict[str, Any]
    blocked: list[dict[str, Any]] = field(default_factory=list)


def _cluster_key(category: str, name: str) -> str:
    return f"{category}::{name}"


def apply_decisions(
    *,
    graph: GraphBuildResult,
    decisions: Mapping[str, str],
    known_same_pairs: Iterable[tuple[str, str, str]] = (),
) -> AlignmentOutcome:
    """把三态裁决落到簇：只有 SAME 触发合并，DIFFERENT 阻止传递合并。

    `known_same_pairs` 为 `(category, name_a, name_b)`，用于把规则层已确定的同义关系
    也纳入同一簇（保持旧的合并语义）。
    """
    clusters = ConflictSafeClusters()
    for edge in graph.edges.values():
        clusters.add(_cluster_key(edge.category, edge.name_a))
        clusters.add(_cluster_key(edge.category, edge.name_b))

    # 第一遍：先登记全部 DIFFERENT 约束，再做任何合并，
    # 否则「A~B、B~C 先合并，之后才发现 A⊥C」这类传递冲突会漏检。
    same_edges: list[tuple[float, str]] = []
    different_count = uncertain_count = same_count = 0
    for edge in graph.edges.values():
        state = decisions.get(edge.pair_id, UNCERTAIN)
        if state == SAME:
            same_count += 1
            same_edges.append((edge.score, edge.pair_id))
        elif state == DIFFERENT:
            different_count += 1
            clusters.mark_different(
                _cluster_key(edge.category, edge.name_a),
                _cluster_key(edge.category, edge.name_b),
            )
        else:
            uncertain_count += 1

    # 第二遍：证据强的边先合并，冲突时保留独立
    for _, pair_id in sorted(same_edges, key=lambda item: (-item[0], item[1])):
        edge = graph.edges[pair_id]
        clusters.union(
            _cluster_key(edge.category, edge.name_a),
            _cluster_key(edge.category, edge.name_b),
            reason=f"llm_same:{pair_id}",
        )
    for category, left, right in known_same_pairs:
        clusters.union(_cluster_key(category, left), _cluster_key(category, right), reason="forced_same")

    members = clusters.cluster_members()
    metrics = {
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "components": len(graph.components),
        "orphans": len(graph.orphans),
        "same": same_count,
        "different": different_count,
        "uncertain": uncertain_count,
        "clusters": len(members),
        "merged_entities": sum(len(group) - 1 for group in members.values()),
        "blocked_merges": len(clusters.blocked),
    }
    return AlignmentOutcome(
        clusters=clusters,
        pair_states=dict(decisions),
        deferred=list(graph.deferred),
        metrics=metrics,
        blocked=list(clusters.blocked),
    )


# ---------------------------------------------------------------------------
# 批量执行
# ---------------------------------------------------------------------------


@dataclass
class JudgeRunStats:
    requests: int = 0
    failed_requests: int = 0
    retries: int = 0
    json_failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "failedRequests": self.failed_requests,
            "retries": self.retries,
            "jsonFailures": self.json_failures,
            "estimatedInputTokens": self.input_tokens,
            "estimatedOutputTokens": self.output_tokens,
            "elapsedSeconds": round(self.elapsed_seconds, 2),
        }


async def run_request_batch(
    *,
    client: Any,
    graph: GraphBuildResult,
    shards: Sequence[RequestShard],
    config: AlignmentConfig,
    progress: Callable[[int, int], None] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> tuple[dict[str, str], dict[str, ComponentDecision], JudgeRunStats]:
    """并发执行所有请求：单请求失败只把该请求的候选边标为 UNCERTAIN，不丢实体。"""
    stats = JudgeRunStats()
    pair_states: dict[str, str] = {}
    component_decisions: dict[str, ComponentDecision] = {}
    started = time.monotonic()
    semaphore = asyncio.Semaphore(max(1, config.parallel))
    done = 0
    lock = asyncio.Lock()

    async def _sleep(seconds: float) -> None:
        if sleep is not None:
            result = sleep(seconds)
            if asyncio.iscoroutine(result):
                await result
            return
        await asyncio.sleep(seconds)

    async def _one(shard: RequestShard) -> None:
        nonlocal done
        async with semaphore:
            stats.requests += 1
            prompt = build_request_prompt(graph, shard)
            stats.input_tokens += _estimate_tokens(prompt)
            succeeded = False
            for attempt in range(1, config.max_attempts_per_request + 1):
                try:
                    payload = await client.generate_json(prompt=prompt, system=ALIGNMENT_SYSTEM_PROMPT)
                    decision = validate_request_decision(payload, shard, graph)
                except Exception:  # noqa: BLE001 - 单请求失败不得中断整轮
                    if attempt < config.max_attempts_per_request:
                        stats.retries += 1
                        stats.json_failures += 1
                        await _sleep(min(8.0, 2.0 ** (attempt - 1)))
                        continue
                    break
                stats.output_tokens += _estimate_tokens(payload)
                async with lock:
                    component_decisions.update(decision.components)
                    pair_states.update(decision.pair_states)
                succeeded = True
                break
            if not succeeded:
                stats.failed_requests += 1
                async with lock:
                    for edge_id in shard.edge_ids:
                        pair_states.setdefault(edge_id, UNCERTAIN)
            async with lock:
                done += 1
                if progress is not None:
                    try:
                        progress(done, len(shards))
                    except Exception:  # noqa: BLE001 - 进度回调不得影响裁决
                        # 进度回调是观测层，抛异常不能拖垮正在跑的裁决；
                        # 记 debug 便于排查「进度停了但任务还在跑」。
                        LOGGER.debug("对齐进度回调失败，已忽略", exc_info=True)

    if shards:
        await asyncio.gather(*[_one(shard) for shard in shards])
    stats.elapsed_seconds = time.monotonic() - started
    return pair_states, component_decisions, stats


def run_alignment_sync(
    *,
    client: Any,
    graph: GraphBuildResult,
    shards: Sequence[RequestShard],
    config: AlignmentConfig,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, str], dict[str, ComponentDecision], JudgeRunStats]:
    """同步包装，供 worker 线程调用。"""
    return asyncio.run(run_request_batch(
        client=client,
        graph=graph,
        shards=shards,
        config=config,
        progress=progress,
    ))


def summarize_graph(graph: GraphBuildResult) -> dict[str, Any]:
    """图结构摘要（用于日志与产物指标）。"""
    sizes = sorted((component.size for component in graph.components), reverse=True)
    return {
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "components": len(graph.components),
        "orphans": len(graph.orphans),
        "largest_component": sizes[0] if sizes else 0,
        "size_distribution": {
            "2-4": sum(1 for size in sizes if 2 <= size <= 4),
            "5-9": sum(1 for size in sizes if 5 <= size <= 9),
            "10+": sum(1 for size in sizes if size >= 10),
        },
        "deferred": len(graph.deferred),
    }

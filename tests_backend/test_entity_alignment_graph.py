"""候选图 + 批量三态裁决的单元测试（不联网：用假 ChatClient 回应协议）。"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.backend_v2.insight.entity_alignment import (
    DEFERRED_GENERIC_TERM,
    DEFERRED_LOW_FREQUENCY,
    DIFFERENT,
    SAME,
    UNCERTAIN,
    AlignmentProtocolError,
    EntityNode,
    apply_decisions,
    build_alignment_graph,
    edge_pair_id,
    build_request_prompt,
    plan_requests,
    resolve_alignment_config,
    run_request_batch,
    validate_request_decision,
)


# ---------------------------------------------------------------------------
# 假客户端：从提示词里读回输入，按策略产出协议响应
# ---------------------------------------------------------------------------


def _prompt_payload(prompt: str) -> dict:
    _, _, tail = prompt.partition("## 输入")
    body = tail.split("## 裁决规则")[0].strip()
    return json.loads(body)


def _response_for(payload: dict, decide) -> dict:
    categories = {entity["id"]: entity["category"] for entity in payload["entities"]}
    components = []
    for component in payload["components"]:
        parent: dict[str, str] = {}

        def find(item: str) -> str:
            parent.setdefault(item, item)
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(left: str, right: str) -> None:
            root_l, root_r = find(left), find(right)
            if root_l != root_r:
                parent[max(root_l, root_r)] = min(root_l, root_r)

        different: list[str] = []
        uncertain: list[str] = []
        for pair in component["candidate_pairs"]:
            state = decide(categories.get(pair["a"], ""), pair["a"], pair["b"])
            if state == SAME:
                union(pair["a"], pair["b"])
            elif state == DIFFERENT:
                different.append(pair["pair_id"])
            else:
                uncertain.append(pair["pair_id"])

        groups: dict[str, list[str]] = {}
        for entity in component["entity_ids"]:
            groups.setdefault(find(entity), []).append(entity)
        merged = [sorted(members) for members in groups.values() if len(members) > 1]
        merged_ids = {member for members in merged for member in members}
        components.append({
            "component_id": component["component_id"],
            "groups": [
                {
                    "canonical_entity_id": members[0],
                    "member_entity_ids": members,
                    "reason_code": "ALIAS_MATCH",
                }
                for members in merged
            ],
            "different_pair_ids": different,
            "uncertain_pair_ids": uncertain,
            "independent_entity_ids": [
                entity for entity in component["entity_ids"] if entity not in merged_ids
            ],
        })
    return {"components": components}


class FakeJudgeClient:
    """按策略作答的假 LLM：记录收到的提示词，可注入失败次数。"""

    def __init__(self, decide=None, fail_times: int = 0) -> None:
        self.prompts: list[str] = []
        self.decide = decide or (lambda _category, _a, _b: UNCERTAIN)
        self.fail_times = fail_times

    async def generate_json(self, *, prompt: str, system: str | None = None) -> dict:
        self.prompts.append(prompt)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("upstream 502")
        return _response_for(_prompt_payload(prompt), self.decide)


def _nodes(category: str, names, pages: int = 4) -> list[EntityNode]:
    return [
        EntityNode(
            node_id=f"{category}:{name}",
            category=category,
            name=name,
            distinct_pages=pages,
            pages=tuple(range(1, pages + 1)),
        )
        for name in names
    ]


def _graph(names, *, pages: int = 4, link=None, max_nodes: int = 24):
    config = resolve_alignment_config(max_nodes_per_request=max_nodes, top_k_per_node=6)
    return build_alignment_graph(
        category="characters",
        nodes=_nodes("characters", names, pages=pages),
        candidate_filter=link or (lambda _cat, _a, _b: _a[:2] == _b[:2]),
        config=config,
    ), config


def _run(graph, shards, config, client):
    return asyncio.run(
        run_request_batch(
            client=client,
            graph=graph,
            shards=shards,
            config=config,
            sleep=lambda _seconds: None,
        )
    )


# ---------------------------------------------------------------------------
# 图组织
# ---------------------------------------------------------------------------


def test_candidate_graph_links_by_filter_and_keeps_orphans() -> None:
    graph, _config = _graph(["虎杖悠仁", "虎杖", "伏黑惠", "钉崎野蔷薇"])

    assert len(graph.components) == 1
    assert {node.name for node_id in graph.components[0].node_ids for node in [graph.nodes[node_id]]} == {
        "虎杖悠仁",
        "虎杖",
    }
    assert [graph.nodes[node_id].name for node_id in graph.orphans] == ["伏黑惠", "钉崎野蔷薇"]
    # 孤立节点依然留在图里（不物理删除任何原始实体）
    assert {node.name for node in graph.nodes.values()} == {"虎杖悠仁", "虎杖", "伏黑惠", "钉崎野蔷薇"}


def test_low_frequency_concepts_are_deferred_but_kept() -> None:
    config = resolve_alignment_config(concept_min_pages=3)
    graph = build_alignment_graph(
        category="concepts",
        nodes=_nodes("concepts", ["杠杆", "爆仓"], pages=1),
        candidate_filter=lambda _cat, _a, _b: True,
        config=config,
    )

    assert graph.edges == {}
    assert graph.components == []
    assert {item["reason"] for item in graph.deferred} == {DEFERRED_LOW_FREQUENCY}
    assert {node.name for node in graph.nodes.values()} == {"杠杆", "爆仓"}


def test_bare_generic_location_terms_are_deferred() -> None:
    config = resolve_alignment_config()
    graph = build_alignment_graph(
        category="locations",
        nodes=_nodes("locations", ["教室", "室内"], pages=5),
        candidate_filter=lambda _cat, _a, _b: True,
        config=config,
    )

    assert graph.edges == {}
    assert {item["reason"] for item in graph.deferred} == {DEFERRED_GENERIC_TERM}


# ---------------------------------------------------------------------------
# 请求组织
# ---------------------------------------------------------------------------


def test_request_packing_respects_budgets_and_covers_all_edges() -> None:
    names = [f"久留美{index}" for index in range(1, 19)]
    graph, config = _graph(names, link=lambda _cat, a, b: a[:3] == b[:3])
    shards = plan_requests(graph=graph, config=config)

    assert shards
    covered_edges = [edge_id for shard in shards for edge_id in shard.edge_ids]
    assert sorted(covered_edges) == sorted(graph.edges)
    assert {node_id for shard in shards for node_id in shard.node_ids} == {
        node.node_id for node in graph.nodes.values()
    }
    assert all(len(shard.node_ids) <= config.max_nodes_per_request for shard in shards)
    assert all(len(shard.edge_ids) <= config.max_edges_per_request for shard in shards)


def test_oversized_component_is_split_with_shared_boundary_entities() -> None:
    names = [f"角色{index:02d}" for index in range(1, 21)]
    graph, config = _graph(names, link=lambda _cat, _a, _b: True, max_nodes=6)
    shards = plan_requests(graph=graph, config=config)

    assert len(shards) > 1
    assert all(len(shard.node_ids) <= config.max_nodes_per_request for shard in shards)
    # 每个分片都被标记为「大分量的一部分」，且共享边界实体以保持跨片语义
    assert all(shard.split_component_ids for shard in shards)
    covered = {node_id for shard in shards for node_id in shard.node_ids}
    assert covered == {node.node_id for node in graph.nodes.values()}
    covered_edges = {edge_id for shard in shards for edge_id in shard.edge_ids}
    assert covered_edges == set(graph.edges)


def test_request_prompt_lists_every_pair_and_component() -> None:
    graph, config = _graph(["虎杖悠仁", "虎杖"])
    shard = plan_requests(graph=graph, config=config)[0]

    payload = _prompt_payload(build_request_prompt(graph, shard))

    assert {entity["id"] for entity in payload["entities"]} == {"虎杖悠仁", "虎杖"}
    assert payload["components"][0]["component_id"] == graph.components[0].component_id
    assert {pair["pair_id"] for pair in payload["components"][0]["candidate_pairs"]} == set(shard.edge_ids)


# ---------------------------------------------------------------------------
# 裁决落簇
# ---------------------------------------------------------------------------


def test_batch_judgement_merges_same_and_keeps_different_independent() -> None:
    graph, config = _graph(["虎杖悠仁", "虎杖", "伏黑惠", "伏黑"])
    client = FakeJudgeClient(
        decide=lambda _cat, a, b: SAME if a[:2] == b[:2] else DIFFERENT
    )
    states, _components, stats = _run(graph, plan_requests(graph=graph, config=config), config, client)

    outcome = apply_decisions(graph=graph, decisions=states)
    assert stats.requests == 1
    assert outcome.metrics["same"] == 2
    assert outcome.clusters.cluster_of("characters::虎杖悠仁") == outcome.clusters.cluster_of("characters::虎杖")
    assert outcome.metrics["merged_entities"] == 2


def test_transitive_merge_conflict_is_blocked_not_forced() -> None:
    graph, _config = _graph(["A·B", "A·C", "A·D"], link=lambda _cat, _a, _b: True)

    def pair(left: str, right: str) -> str:
        return edge_pair_id("characters", left, right)

    # 跨片/跨请求时模型可能给出彼此矛盾的裁决：A·B~A·C、A·C~A·D 为同一实体，
    # 但 A·B 与 A·D 被明确判为不同 → 传递合并必须被阻止，二者保持独立。
    states = {
        pair("A·B", "A·C"): SAME,
        pair("A·C", "A·D"): SAME,
        pair("A·B", "A·D"): DIFFERENT,
    }
    outcome = apply_decisions(graph=graph, decisions=states)

    assert outcome.metrics["blocked_merges"] >= 1
    assert outcome.blocked
    assert outcome.clusters.cluster_of("characters::A·B") != outcome.clusters.cluster_of("characters::A·D")


def test_uncertain_decisions_do_not_merge() -> None:
    graph, config = _graph(["久留美", "久留美母亲"])
    client = FakeJudgeClient(decide=lambda _cat, _a, _b: UNCERTAIN)
    states, _components, _stats = _run(graph, plan_requests(graph=graph, config=config), config, client)

    outcome = apply_decisions(graph=graph, decisions=states)
    assert outcome.metrics["merged_entities"] == 0
    assert outcome.metrics["uncertain"] == len(graph.edges)


def test_failed_request_degrades_to_uncertain_without_losing_entities() -> None:
    graph, config = _graph(["虎杖悠仁", "虎杖"])
    config = resolve_alignment_config(max_attempts_per_request=1)
    client = FakeJudgeClient(fail_times=1)
    states, _components, stats = _run(graph, plan_requests(graph=graph, config=config), config, client)

    outcome = apply_decisions(graph=graph, decisions=states)
    assert stats.failed_requests == 1
    assert set(states.values()) == {UNCERTAIN}
    assert outcome.metrics["merged_entities"] == 0
    assert {node.name for node in graph.nodes.values()} == {"虎杖悠仁", "虎杖"}


def test_categories_are_never_merged_together() -> None:
    characters = build_alignment_graph(
        category="characters",
        nodes=_nodes("characters", ["东京"]),
        candidate_filter=lambda _cat, _a, _b: True,
        config=resolve_alignment_config(),
    )
    locations = build_alignment_graph(
        category="locations",
        nodes=_nodes("locations", ["东京"], pages=6),
        candidate_filter=lambda _cat, _a, _b: True,
        config=resolve_alignment_config(),
    )
    assert characters.edges == {} and locations.edges == {}
    assert characters.nodes["characters:东京"].category == "characters"
    assert locations.nodes["locations:东京"].category == "locations"


def test_alignment_is_deterministic_across_runs() -> None:
    names = ["久留美", "久留美（主角）", "福贺久留美", "久留美母亲", "萌智子"]
    plan = []
    for _ in range(2):
        graph, config = _graph(names, link=lambda _cat, a, b: a[:3] == b[:3] or a == b)
        shards = plan_requests(graph=graph, config=config)
        client = FakeJudgeClient(decide=lambda _cat, a, b: SAME if "久留美" in a and "久留美" in b else UNCERTAIN)
        states, _components, stats = _run(graph, shards, config, client)
        outcome = apply_decisions(graph=graph, decisions=states)
        plan.append((
            [(shard.request_id, shard.node_ids, shard.edge_ids) for shard in shards],
            sorted(outcome.clusters.cluster_members()),
        ))
    assert plan[0] == plan[1]


# ---------------------------------------------------------------------------
# 协议校验
# ---------------------------------------------------------------------------


def _single_shard():
    graph, config = _graph(["虎杖悠仁", "虎杖"])
    shard = plan_requests(graph=graph, config=config)[0]
    return graph, shard


def test_missing_pair_decision_is_a_protocol_error() -> None:
    graph, shard = _single_shard()
    payload = {
        "components": [{
            "component_id": shard.components[0].component_id,
            "groups": [],
            "different_pair_ids": [],
            "uncertain_pair_ids": [],
            "independent_entity_ids": ["虎杖悠仁", "虎杖"],
        }]
    }
    with pytest.raises(AlignmentProtocolError, match="未全部裁决"):
        validate_request_decision(payload, shard, graph)


def test_conflicting_pair_assignment_is_a_protocol_error() -> None:
    graph, shard = _single_shard()
    pair_id = shard.edge_ids[0]
    payload = {
        "components": [{
            "component_id": shard.components[0].component_id,
            "groups": [{
                "canonical_entity_id": "虎杖",
                "member_entity_ids": ["虎杖", "虎杖悠仁"],
                "reason_code": "ALIAS_MATCH",
            }],
            "different_pair_ids": [pair_id],
            "uncertain_pair_ids": [],
            "independent_entity_ids": [],
        }]
    }
    with pytest.raises(AlignmentProtocolError, match="同时被判为 SAME 与 DIFFERENT"):
        validate_request_decision(payload, shard, graph)


def test_out_of_range_entity_is_a_protocol_error() -> None:
    graph, shard = _single_shard()
    payload = {
        "components": [{
            "component_id": shard.components[0].component_id,
            "groups": [{
                "canonical_entity_id": "伏黑惠",
                "member_entity_ids": ["伏黑惠"],
                "reason_code": "ALIAS_MATCH",
            }],
            "different_pair_ids": [],
            "uncertain_pair_ids": list(shard.edge_ids),
            "independent_entity_ids": [],
        }]
    }
    with pytest.raises(AlignmentProtocolError, match="不在输入范围"):
        validate_request_decision(payload, shard, graph)


def test_alignment_config_reads_environment_overrides(monkeypatch) -> None:
    monkeypatch.setenv("INSIGHT_ALIGN_TOP_K", "9")
    monkeypatch.setenv("INSIGHT_ALIGN_MAX_EDGES", "99999")
    monkeypatch.setenv("INSIGHT_ALIGN_PARALLEL", "0")
    config = resolve_alignment_config()

    assert config.top_k_per_node == 9
    assert config.max_edges_per_request == 2000
    assert config.parallel == 1
    assert resolve_alignment_config(top_k_per_node=2).top_k_per_node == 2

"""U-1：Insight 前置条件错误类的回归守卫。

``InsightConflict`` 与 ``InsightPrecondition`` 都是 409，但 code 不同：

* ``revision_conflict``：别人先改了数据 —— 刷新快照后重试有效。
* ``conflict``：前置条件不满足 —— 需要先完成某个动作（构建知识库 / 配置供应商 /
  清空项目 / 生成脚本），刷新无用。

本文件锁两件事：
1. 配置类前置条件真的抛 ``InsightPrecondition``（真实函数调用，不打桩）。
2. 已知的前置条件文案不允许再被写成裸 ``InsightConflict``（AST 扫描，防回退）。
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

from src.backend_v2.insight.errors import InsightPrecondition
from src.backend_v2.insight.provider_runtime import _credential_values
from src.backend_v2.insight.repository import InsightConflict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSIGHT_DIR = PROJECT_ROOT / "src" / "backend_v2" / "insight"

# 必须走 InsightPrecondition 的前置条件文案（用户需要先做一个动作）。
PRECONDITION_MESSAGES = re.compile(
    r"frozen Insight .* (provider is invalid|credential is missing|base URL is invalid"
    r"|config fields are invalid|credential version is invalid|config is invalid)"
    r"|ChromaDB is not installed"
    r"|published page analysis is stale; reanalyze the page"
    r"|analysis layer has no covered pages"
    r"|analysis run has no successful pages"
    r"|only overview artifacts support direct export"
    r"|unsupported Insight export step"
    r"|generate or save a script first"
    r"|has no pages to illustrate"
    r"|has no active images to export"
    r"|is invalid; clear the project"
    r"|is invalid; delete and recreate"
    r"|页面原图缺失|没有可解析的出场页|不支持的蒸馏类型|没有可用的实体知识"
    r"|该书还没有实体知识网络|该书还没有已发布的页面分析结果|实体知识网络已过期"
)


def test_precondition_is_a_conflict_but_not_the_reverse() -> None:
    assert issubclass(InsightPrecondition, InsightConflict)
    assert not issubclass(InsightConflict, InsightPrecondition)


@pytest.mark.parametrize(
    "section",
    [
        {"provider": "deepseek", "api_key": ""},
        {"provider": "not-a-real-provider", "api_key": "k"},
    ],
)
def test_provider_configuration_failures_are_preconditions(section: dict) -> None:
    with pytest.raises(InsightPrecondition):
        _credential_values(section, name="vlm")


def _raised_message(node: ast.Raise) -> str:
    if not (isinstance(node.exc, ast.Call) and node.exc.args):
        return ""
    arg = node.exc.args[0]
    if isinstance(arg, ast.Constant):
        return str(arg.value)
    if isinstance(arg, ast.JoinedStr):
        return "".join(
            str(v.value) if isinstance(v, ast.Constant) else "{}" for v in arg.values
        )
    return ""


def test_known_precondition_messages_never_raise_bare_insight_conflict() -> None:
    offenders: list[str] = []
    for path in sorted(INSIGHT_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            func = node.exc.func if isinstance(node.exc, ast.Call) else None
            name = func.id if isinstance(func, ast.Name) else None
            if name != "InsightConflict":
                continue
            message = _raised_message(node)
            if PRECONDITION_MESSAGES.search(message):
                offenders.append(
                    f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno} {message[:70]}"
                )
    assert offenders == [], (
        "以下前置条件文案仍然抛裸 InsightConflict，应改为 InsightPrecondition：\n"
        + "\n".join(f"  {item}" for item in offenders)
    )

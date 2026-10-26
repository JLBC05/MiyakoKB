"""守卫：insight 面不允许出现「完全无痕」的裸 except。

背景（审计 P3-3）：清理前的 codebase 里有大量 `except Exception: pass`，
出问题时既没有错误也没有日志，排查只能靠猜。这里把「静默」收窄成两种
可接受形态，其余一律视为回归：

1. 有日志（`LOGGER.*` / `logging.*` / 打印）——留下痕迹即可；
2. 有 `# noqa: BLE001` 注释——显式声明「我知道这里必须吞」，且要求注释里
   说明原因（同一行或紧邻上一行要能读懂为什么）。

同时禁止裸 `except:`（连 KeyboardInterrupt / SystemExit 一起吞掉）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

INSIGHT_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "backend_v2" / "insight"
)

_LOG_CALLS = {"LOGGER", "logging", "logger", "print"}


def _iter_handlers(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            yield node


def _is_broad_swallow(handler: ast.ExceptHandler) -> bool:
    node_type = handler.type
    if node_type is None:
        return True  # 裸 except:
    if isinstance(node_type, ast.Name) and node_type.id in {"Exception", "BaseException"}:
        return True
    if isinstance(node_type, ast.Tuple):
        return any(
            isinstance(item, ast.Name) and item.id in {"Exception", "BaseException"}
            for item in node_type.elts
        )
    return False


def _body_is_only_pass(handler: ast.ExceptHandler) -> bool:
    return len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)


def _mentions_logging(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id in _LOG_CALLS:
                    return True
        if isinstance(node, ast.Name) and node.id in _LOG_CALLS:
            return True
    return False


def _source_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _noqa_declared(lines: list[str], handler: ast.ExceptHandler) -> bool:
    """handler 是否显式声明 noqa: BLE001，并在附近写明了原因。

    原因可以写在 noqa 同一行的行尾，也可以写在 handler 内紧随其后的注释行
    （长解释放在下一行更易读）。
    """
    except_index = handler.lineno - 1
    if not (0 <= except_index < len(lines)):
        return False
    line = lines[except_index]
    if "noqa: BLE001" not in line:
        return False
    comment = line.split("#", 1)[-1]
    if len(comment) > len("noqa: BLE001") + 3:
        return True
    # 行尾只有 noqa 本身时，要求紧随其后有真正的说明注释（不止是 pass）。
    for offset in range(1, 4):
        index = except_index + offset
        if index >= len(lines):
            break
        candidate = lines[index].strip()
        if candidate.startswith("#") and len(candidate) > 2:
            return True
        if candidate and not candidate.startswith("#"):
            break
    return False


def _insight_modules() -> list[Path]:
    return sorted(INSIGHT_DIR.glob("*.py"))


def test_insight_modules_exist() -> None:
    # 守卫本身要有对象可查，否则文件改名后这条测试会静默变绿。
    modules = _insight_modules()
    assert modules, f"在 {INSIGHT_DIR} 没找到任何模块"
    assert any(module.name == "derived.py" for module in modules)


def test_no_bare_except_in_insight() -> None:
    offenders: list[str] = []
    for module in _insight_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for handler in _iter_handlers(tree):
            if handler.type is None:
                offenders.append(f"{module.name}:{handler.lineno}")
    assert not offenders, "存在裸 except（会连 KeyboardInterrupt 一起吞）：\n" + "\n".join(
        offenders
    )


def test_silent_swallows_declare_why() -> None:
    offenders: list[str] = []
    for module in _insight_modules():
        lines = _source_lines(module)
        tree = ast.parse("\n".join(lines))
        for handler in _iter_handlers(tree):
            if not _is_broad_swallow(handler):
                continue
            if _mentions_logging(handler):
                continue
            if not _body_is_only_pass(handler):
                continue
            if _noqa_declared(lines, handler):
                continue
            offenders.append(f"{module.name}:{handler.lineno}")
    assert not offenders, (
        "这些 broad except 完全无痕：既没有日志，也没有带原因的 noqa: BLE001。"
        "要么记一条日志（debug 也行），要么写明为什么必须吞：\n" + "\n".join(offenders)
    )


def test_guard_detects_a_silent_swallow(tmp_path: Path) -> None:
    """自检：守卫真的能抓住新增的静默 except（否则等于没生效）。"""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "try:\n    risky()\nexcept Exception:\n    pass\n", encoding="utf-8"
    )
    lines = _source_lines(sample)
    tree = ast.parse("\n".join(lines))
    handlers = list(_iter_handlers(tree))
    assert len(handlers) == 1
    assert _is_broad_swallow(handlers[0])
    assert not _mentions_logging(handlers[0])
    assert _body_is_only_pass(handlers[0])
    assert not _noqa_declared(lines, handlers[0])


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("try:\n    risky()\nexcept Exception:\n    pass\n", False),
        ("try:\n    risky()\nexcept Exception:\n    LOGGER.warning('x')\n", True),
        ("try:\n    risky()\nexcept Exception:  # noqa: BLE001 - 清理失败不该掩盖原异常\n    pass\n", True),
        ("try:\n    risky()\nexcept ValueError:\n    pass\n", True),
        ("try:\n    risky()\nexcept Exception:\n    raise\n", True),
    ],
)
def test_noqa_and_logging_are_the_only_accepted_forms(
    tmp_path: Path, source: str, expected: bool
) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")
    lines = _source_lines(sample)
    handler = next(iter(_iter_handlers(ast.parse("\n".join(lines)))))
    clean = (
        not _is_broad_swallow(handler)
        or _mentions_logging(handler)
        or not _body_is_only_pass(handler)
        or _noqa_declared(lines, handler)
    )
    assert clean is expected

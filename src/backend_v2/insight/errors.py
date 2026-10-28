"""Insight 域的异常语义分层（U-1）。

背景：``InsightConflict`` 长期以来同时承载两类语义完全不同的失败：

1. **乐观锁冲突**（别人先改了数据，重试即可）——典型是 ``InsightLocked``、
   ``continuation.py`` 的 revision 比较失败。
2. **前置条件不满足**（状态不对，重试无用）——典型是「该书还没有实体知识网络」
   「还没有已发布的页面分析结果」。

两者此前都经 ``insight/routes.py`` 的 ``InsightConflict`` 处理器映射成
``409 revision_conflict``，前端据此只会去"刷新快照再试"，于是第 2 类永远刷
不出来，用户看到的是无限「已载入最新版本」。

``InsightPrecondition`` 用来区分第 2 类。它继承 ``InsightConflict``，
所以尚未迁移的调用点、以及任何 ``except InsightConflict`` 的旧代码都保持原有
行为；只有当路由层注册了更具体的处理器时，它才会映射到 ``409 conflict``。
"""

from __future__ import annotations

from .repository import InsightConflict

__all__ = ["InsightPrecondition"]


class InsightPrecondition(InsightConflict):
    """请求所依赖的事实前置不满足（状态不对，重试无用）。

    与父类 ``InsightConflict``（乐观锁冲突，重试有用）互斥使用。判断标准：
    消息描述的是"还没有 / 尚未 / 缺少 / 需要先完成 X"这类**状态**问题，且
    调用点不在 ``with_for_update()`` 或 revision 比较的上下文里。
    """

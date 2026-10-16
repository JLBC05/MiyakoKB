# 项目开发进展报告 (PROGRESS.md)

## 1. 项目基本信息
- **项目名称**: `MiyakoKB`
- **项目路径**: `D:\My_Doc\Mine\MiyakoKB` (当前工作区: `Saber-Translator-main`)
- **远端仓库**: `https://github.com/JLBC05/MiyakoKB.git` (分支: `main`)

---

## 2. 核心模块与进展状态

### 2.1 已完成 (Completed)
1. **实体 Slug 映射分离 (`entity_slug_map.json`)**:
   - 将原硬编码在代码中的实体罗马音拆为外部 JSON 配置文件，支持 `characters`, `items`, `abilities`, `locations` 四大类。
   - 实现方案 A：命中映射表的实体生成标准罗马音 ID；未命中的未知实体自动回退到拼音并添加 `pinyin_` 显式标记（如 `item_pinyin_tie_chui`）。
   - 提供 `list_pinyin_entities(book_id, category=None)` 快速审计待补全实体的接口。

2. **别名种子与章节覆盖 (`alias_seed.json`)**:
   - 支持跨章节全局同义词 `global_terms`。
   - 支持特定章节生效的 `chapter_overrides`（如第 1 话将“特级咒物”映射到“宿傩之指”，后续话数保持通用咒物独立性）。

3. **实体对齐引擎核心 (`src/backend_v2/insight/derived.py`)**:
   - `InsightDerivedRepository.align_entities()` 支持单事务级行锁/版本更新，维护 `revision`。
   - 支持 Sticky IDs（粘性 ID）跨章节继承，`valid_chapters` 累积追踪。
   - `validate_artifact_payload` 支持 `entity_alignment` 产物结构。

4. **全自动归并机制落地 (Auto-Merge)**:
   - **取消人工审核队列**: 彻底移除 `pending_review_queue`，替换为全自动 `auto_merge_log`。
   - **三条自动归并规则**:
     1. 频次主导（Frequency Dominant）：同类别内高频主导，频次差 >= 2 倍时自动归并。
     2. 子串重合（Substring Overlap）：同类别内字符重合度 >= 60% 自动归并（如“宿傩之指”与“宿傩的手指”）。
     3. 模型辅助（Model Assisted）：重合度 40%-60% 的模糊对支持轻量级大模型判定。
   - **非破坏性设计**: 页面原始 `payload_json` 保持不变，产物中完整保留 `canonical_name`、`aliases` 与 `original_names`，保证原始名称永不丢失。
   - **回滚接口**: 实现 `unmerge_alias(book_id, category, alias_to_detach)`，支持一键将误归并别名拆出并创建独立实体。

5. **第 1-5 话增量累积验证通过**:
   - 核心角色 ID 100% 保持稳定：虎杖悠仁 (`character_itadori_yuji`)、伏黑惠 (`character_fushiguro_megumi`)、五条悟 (`character_gojo_satoru`)、钉崎野蔷薇 (`character_kugisaki_nobara`)、两面宿傩 (`character_ryomen_sukuna`)。
   - 自动归并记录完整追踪（累计记录 44 次决策），`pinyin_` 实体通过自动归并从 23 个大幅缩减至 16 个。

6. **`SLUG_MODE` 写入 `analysis_runs.config_json` 与全链路贯通**:
   - **权威来源与收敛**: 确立 `analysis.slugMode` 为唯一权威来源，根层 `slugMode` 作为严格同步的向后兼容别名。合法枚举规范为 `"pinyin"` 与 `"romanized"`（同时兼容历史别名 `"english"`）。
   - **默认配置与校验**: `DEFAULT_INSIGHT_SETTINGS` 默认包含 `"slugMode": "pinyin"`，`validation.py` 与 `resolver.py` 严格校验并解析 `slugMode`。
   - **指令支持与持久化**: `InsightAnalysisCommandService` 支持通过 analysis command 指定 `slugMode`；`InsightRepository.insert_run` 将配置固化写入 `analysis_runs.config_json`。
   - **安全提取接口**: 在 `repository.py` 与 `page_schema.py` 提供 `get_run_slug_mode` 工具函数，严格遵循 `AGENTS.md` 规范读取 `analysis.slugMode`。
   - **对齐引擎联动与实体纯拼音化**: `derived.py` 的 `align_entities` 优先通过 `run_id` 继承权威 `slug_mode`；在拼音模式（`pinyin`）下生成不带冗余前缀的纯拼音 ID（如 `character_hu_zhang_you_ren`），消除历史残留的 `pinyin_` 杂质；`romanized` 模式下未映射实体回退并打上 `romanized_` 标记；`list_pinyin_entities` 与 `unmerge_alias` 完整适配。
   - **全链路自动化测试与验证**:
     * 单元测试 `test_insight_slug_mode_written_to_analysis_runs_config_json` 100% 通过。
     * 第 1 话完整回测验证通过：`revision=1`, `entityCount=24`，产物 `slug_mode='pinyin'`，核心角色 ID 纯拼音化且 `_pinyin_` 残留数为 0。

---

### 2.2 进行中 (In Progress)
- 暂无进行中阻塞项，准备开展跨作品通用性测试。

---

### 2.3 未完成与后续计划 (To Do)
1. **第 2 部漫画的跨作品通用性验证**:
   - 在新漫画集（不同类型题材，如恋爱/悬疑）上测试无需任何预置 alias_seed 的冷启动表现与自动归并准确率。
2. **`pinyin_` 实体的罗马音补全**:
   - 针对 1-5 话中剩余的 16 个未知实体（如西久福、爆米花、沙织等），在 `entity_slug_map.json` 中补齐其官方罗马音或英文术语。

---

## 3. 已知问题与修复记录 (Known Issues)
1. **`verify_insight_rules.py` 误提交问题**:
   - **状态: 已彻底修复**。已执行 `git rm --cached verify_insight_rules.py` 并在 commit `8915080` 中移出版本控制，后续已通过 `git ls-files` 验证不再跟踪。
2. **`pinyin_` 标记实体数量**:
   - 原有 23 个未知实体，在三条自动归并规则应用后自动合并了 7 个变体，剩余 16 个实体属于单次出现的专有名词，待配置文件扩充。

---

## 4. 关键文件索引
- 核心实体对齐与自动归并逻辑: `src/backend_v2/insight/derived.py`
- 实体罗马音映射字典: `entity_slug_map.json`
- 种子别名与章节覆盖规则: `alias_seed.json`
- 项目开发规范与数据库防手写约束: `AGENTS.md`
- 验证脚本:
  - 1-5 话累积与自动归并回归: `verify_automerge_scheme.py`
  - 单话知识提取脚本: `run_full_chapter_1.py` ~ `run_full_chapter_5.py`

---

## 5. 数据库位置与 `analysis_artifacts` 读取方式

### 5.1 数据库位置
- **生产/默认 SQLite 库**: `Saber-Translator-main` 运行时的默认 SQLite 配置或外部配置的 DB（当前 1-5 话全量验证数据库为 `test_chapters_1_to_5_automerge.db`）。

### 5.2 `analysis_artifacts` 表的读取方式
依据 `AGENTS.md` 规范，**严禁手写表名和字段字符串**，必须通过 schema 对象进行安全查询：

```python
from sqlalchemy import select
from src.backend_v2.storage.schema import analysis_artifacts
from src.backend_v2.insight.derived import InsightDerivedRepository

# 方式一：推荐使用 InsightDerivedRepository 接口直接读取
repo = InsightDerivedRepository(engine)
artifact = repo.get_artifact(book_id=BOOK_ID, kind="entity_alignment", template="default")
if artifact:
    payload = artifact["payload"]
    canonical_entities = payload.get("canonical_entities", [])
    alias_map = payload.get("alias_map", {})
    auto_merge_log = payload.get("auto_merge_log", [])

# 方式二：使用 SQLAlchemy Core 原生安全查询
with engine.connect() as conn:
    stmt = (
        select(
            analysis_artifacts.c.id,
            analysis_artifacts.c.revision,
            analysis_artifacts.c.payload_json,
            analysis_artifacts.c.updated_at,
        )
        .where(
            analysis_artifacts.c.book_id == BOOK_ID,
            analysis_artifacts.c.kind == "entity_alignment",
            analysis_artifacts.c.template == "default",
        )
    )
    row = conn.execute(stmt).fetchone()
    if row:
        import json
        payload = json.loads(row.payload_json)
```

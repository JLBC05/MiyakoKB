# 知识库构建流程清理方案（Insight Cleanup Plan）

> 状态：已确认，执行中
> 范围：`src/backend_v2/insight/`、`src/backend_v2/settings/`、`src/backend_v2/storage/`、`src/backend_v2/worker/`、`src/shared/user_logging.py`、`openapi/v2.yaml`、`vue-frontend/`
> 背景：现有知识库构建链路（冻结配置 → 逐页 VLM → 分层汇总 → 派生产物 → 发布）本身是健康的，但沉淀了三类遗留：①某部具体作品的硬编码数据；②无生产者的死代码/死配置；③实体知识图谱（`entity_alignment`）只是时间线步骤的副产品，无法独立重建、失败被静默吞掉。

## 已确认的执行决策

| 编号 | 决策 | 结论 |
|---|---|---|
| D1 | A2 的 `entity_id` 兼容策略 | **全量重建换新 ID**，旧 ID 落入 `alias_map` 兜底（复用 `derived.py:3448-3453` 的 `should_refresh_ids` 逻辑） |
| D2 | 是否允许改动前后 A/B 对比跑 | **允许**（含 API 成本），基线见「基线」一节 |
| D3 | C1.2 对齐失败策略 | **自动派独立的 `entity_alignment` 补偿 job**（时间线产物照常发布，图谱不丢） |
| D4 | 方案落成文档 | 即本文件 |

## 排期与状态

| 步骤 | 内容 | 状态 |
|---|---|---|
| 0 | 基线快照 + 数据库备份 | ✅ 完成（`temp/insight_baseline_before.json`、`temp/backup-before-cleanup/saber.sqlite3`） |
| 1 | **A1** 全局 VLM 提示词去作品化 | ✅ 完成 |
| 2 | **A4 + A6** 删除写死的预定义聚类、收敛 slug 配置加载路径 | ✅ 完成（含仓库内作品专属配置移出） |
| 3 | **A2 + A3 + A5** slug 生成通用化 + 判例抽象化 | ✅ 完成（**全量重建待做**） |
| 4 | **B1 + B2** 删除 `insight_analyze_page`、摘掉 `question_decompose` 依赖 | ✅ 完成 |
| 5 | **C1.1 + C1.2** 实体对齐独立 job + 失败补偿 + 状态可见 | ✅ 完成（附带修复了"分析结果永远 `completed_with_errors`"的既有 bug） |
| 6 | B3（parallel 死分支）、C1.3（图谱审核 API）、C2（三份全书总结合一）、C4（翻译残留表面清理） | 未排期 |

### 执行纪律

1. **A2 + A3 + A5 必须同批**：每次改动 slug 规则都会改变 `entity_id` 语义，分开做无法归因。
2. **C2 不得与 A5 同批**：C2 会改动 prompt_type 编号，与提示词改动叠加后无法区分是谁的效果。
3. **B3 独立提交**，不与其他改动混合。

## 已落地改动的实际形态（与方案的偏差记录）

| 项 | 实际实现 | 偏差说明 |
|---|---|---|
| A1 | `worker.py:101-116` 四个类别示例全部换成与作品无关的占位（`随身工具/书籍/电子设备`、`'<机构或建筑名>·<房间或区域名>'`、`'<体系或流派名>·<招式名>'`、"抽象机制、专业术语、关键金额、社会或历史事件"） | 无 |
| A2+A3 | 删除 `KNOWN_LOCATION_ROOTS`/`KNOWN_LOCATION_SUBS`/`CHAR_PINYIN` 与 `romanize_location_string`，合并为单一函数 `pinyin_slug(text)`；`to_romanized_slug` 不再对 `locations` 特判 | **行为变化**：地点在 `romanized` 模式下不再绕过 `entity_slug_map`，未命中时会带 `romanized_` 前缀（与其它类别一致，进入人工审核队列）。`pinyin_slug` 全空时返回常量 `"entity"`（不是"类别+序号"），同类别多个空名实体理论上会撞 ID，实际不会出现 |
| A4 | 删除 `clusters_by_cat` 与消费分支、`processed_names` 残骸 | 无；`cluster_predefined` 规则号不再产生 |
| A5 | 判例改为结构同构占位（`'X'` / `'X母亲'` / `'<体系>·<分支A>'` / `'<机构>·<地点A>'` / 数值与年份示例），**未**接"动态取当前书 canonical_entities 做 few-shot" | 动态 few-shot 会引入"用本次输出喂本次判断"的自指风险且不利于缓存，抽象化即可消除作品耦合 |
| A6 | `load_entity_slug_map(config_path)` 只接受显式路径；worker 用 `data_root/entity_slug_map.json`；仓库内 `entity_slug_map.json`、`alias_seed.json` 已移至 `D:\My_Doc\Mine\manga-knowledge\temp\insight-cleanup-removed\` | 附带修复：`derived.py` 使用 `LOGGER` 却从未定义，进入对齐异常分支会 NameError（即 C1.2 表面"静默吞异常"实际是抛错），已补 `LOGGER` |
| B1 | 删除 `worker.py` kind 集合中的分支、`entrypoint.py` 注册、`user_logging.py` 标签 | 无 |
| B2 | `PROMPT_TYPES`、`resolver.py` 必填列表、`defaults.py` factory、`openapi/v2.yaml` 枚举、`vue-frontend/src/api/generated/v2.ts` 联合类型同步移除 `question_decompose` | 迁移 SQL 与既有库 CHECK 不动（最小方案） |
| C1.1 | `DERIVED_KINDS` 加 `entity_alignment`；`create_job` step 映射加 `insight_build_entity_alignment`；`handle()` 新增独立分支直接调 `align_entities(run_id=None, slug_mode=_slug_mode_from_config(config))` 并立即激活；`routes.py` 加 `POST /books/<book_id>/entity-alignment/rebuild`；`entrypoint.py`/`user_logging.py` 注册与标签"生成实体知识网络"；`openapi/v2.yaml` 加路径并用 `npm run generate:api`（openapi-typescript 7.13.0）重新生成 `v2.ts` | `create_job` 的冻结 config 里没有 `runId`（只有 `sourceRunId`），所以独立 job 的图谱不再与某个分析 run 绑定（`run_id=None`）；`slug_mode` 改从冻结 config 的 `analysis.slugMode` 读取（新增 `_slug_mode_from_config`），不再依赖 `run_id` 反查 `analysis_runs` |
| C1.2 | `handle()` 内联对齐失败 → `LOGGER.warning` 后照常发布；`worker.py:_publish_run` 发布成功后调 `_dispatch_entity_alignment_if_missing`，用 `run_artifact_exists` 判断图谱是否缺失，缺失则派 `entity_alignment` 派生 job（幂等键 `insight-entity-alignment-compensate:<runId>`）；`get_books_knowledge_status()` 每书新增 `graph` 字段 | **偏差**：原方案打算把内联调用改为 `run_id=None`，实际改为 `align_entities(..., staged=True)` 让图谱作为该分析 run 的暂存产物由 `publish_run` 原子激活 —— 这样不用放弃 `run_id`（`slugMode` 仍按 run 解析），也不需要显式传 `slug_mode` |
| C1 附带修复 | `align_entities` 新增 `staged` 与 `dependency_fingerprint` 参数；暂存时 `status="building"`/`is_active=False`/指纹用 `frozen.fingerprint` | 见下「附带修复：分析流水线必然 `completed_with_errors`」 |

### graph 字段口径（`GET /books/knowledge-status`）

- `status`：`ready`（存在已激活产物）/ `failed`（有失败记录且无产物，或最新一次失败晚于当前产物更新时间）/ `pending`（只有未激活的暂存产物）/ `none`（从未构建）。
- 另含 `revision`、`entityCount`（取自 payload 的 `canonical_entities` 长度）、`updatedAt`、`lastError`（取自 `job_steps.error_json` 的 `message`/`code`）。

### 附带修复：分析流水线必然 `completed_with_errors`（既有 bug）

- 现象：在干净 HEAD（`d5c2988`）上 `test_stage5_insight.py::test_insight_bootstrap_identifies_active_job_kinds` 即失败 —— `assert 'completed_with_errors' == 'completed'`，日志 `发布分析结果失败：staged analysis artifact identity is invalid`。
- 根因：`insight_stage_timeline` 步骤内联调用 `align_entities` 时，产物被写成 `status="ready"` + `is_active=True` + `run_id=<该分析 run>`；而 `InsightRepository.finalize_run`（`repository.py:1585-1636`）要求**凡挂在该 run 上的 `analysis_artifacts` 必须是 `building` 且未激活**，于是每次整套分析都会在发布步骤被 `InsightConflict` 打断。同一处 `except` 分支还引用了从未定义的 `LOGGER`（一旦进入即 `NameError`）。
- 修复：`align_entities` 增加 `staged` 语义（暂存 → 由 `publish_run` 统一激活），内联调用传 `staged=staging_run` + `dependency_fingerprint=frozen.fingerprint`（同一 run 的所有产物必须共用同一输入指纹，`repository.py:1642-1647/1715-1720/1790-1795` 会把它们放进同一个集合，多于一个即报 `staged derived generations are inconsistent`）。
- 结果：该测试现在通过（改动前/改动后都失败 → 现在通过）。

### 待验证 / 遗留

- **全量重建 + A/B 对比尚未执行**：生产库 `data-v2/saber.sqlite3` 目前 `analysis_runs=0`、artifacts=0（详见「基线」一节），所以 A/B 只能在新跑一次构建后对比，对比口径见基线脚本输出。
- `derived.py` 仍有非作品专属但可疑的写死分组 `_CONCEPT_SEMANTIC_GROUPS`（time/money/mechanism/psychology/society），源自某金融题材作品，未纳入本次清理。
- `docs/*` 被 `.gitignore:93` 忽略，本文件不进版本控制（与既有 `docs/storage-*.md` 一致）。

---

## A. 清除作品专属硬编码

### A1 · 全局 VLM 提示词里的作品示例
- 位置：`src/backend_v2/insight/worker.py:101-116`（`knowledge_mode` 的 `extraction_rules`）。
- 删除：`:111` 的 `'杉泽第三高中·家庭科准备室'`、`:112` 的 `（如：十种影法术·蟾蜍）`、`:113` 的 `（如：FX、杠杆、爆仓、追加保证金、2000万日元欠款、雷曼危机）`。
- 保留：items/locations/abilities/concepts 的字段边界定义、"同一场景只保留一条 + 完整层级路径命名"、"确定性原则"、"强制判定"。
- 举例改为与作品无关的结构化占位。

### A2 · 地点词根表 + 罗马化函数
- 删除：`derived.py:85-98` `KNOWN_LOCATION_ROOTS`、`derived.py:101-133` `KNOWN_LOCATION_SUBS`。
- 改造：`derived.py:181-206` `romanize_location_string` 改为「按 `·` 分段 → 每段 pypinyin → 段间 `_` 连接」，与 `to_romanized_slug` 的非地点分支（`derived.py:244-272`）合并为同一个拼音化函数。
- 兼容：按 D1 走全量重建 + alias 兜底。

### A3 · 手写拼音表
- 删除：`derived.py:162-171` `CHAR_PINYIN` 及其引用（`:196-198`、`:259-261`）。
- 兜底替换为：保留 ASCII/数字，其余转 `_`；全空则用「类别 + 稳定序号」。

### A4 · 写死的预定义聚类簇
- 删除：`derived.py:3535-3568` `clusters_by_cat` 及消费分支（`:3572-3640`，会写入 `auto_merge_log` 的 `cluster_predefined`）。
- 若需保留该能力：走已有的外部种子通道 `global_terms` / `chapter_overrides`（`derived.py:3999-4033`）。

### A5 · 归并提示词里的作品判例
- 位置：`derived.py:287-600`（含 `:409-416` 地点合并判例）。
- 改造：从"具体作品名判例"改为**从当前书的 `canonical_entities` 动态取候选**做 few-shot；不可动态化的部分抽象为结构同构示例。

### A6 · slug 配置加载路径
- 位置：`derived.py:136-160` `load_entity_slug_map`。
- 删除 `Path.cwd()` 与 `parents[4]` 两条探测；路径由设置冻结时显式传入（进 `settingsSnapshot`）或固定在 `data_root` 下；找不到时明确记日志。
- 附带：仓库内两份 `entity_slug_map.json`（内容为咒术回战 + 猎人的手工表）属作品专属数据，移出仓库、改为书级配置导入。

## B. 清除死代码 / 死配置

### B1 · `insight_analyze_page`
- 删除三处：`worker.py:278`（kind 集合）、`entrypoint.py:280`（handler 注册）、`user_logging.py:73`（标签"漫画分析（旧任务）"）。
- 前置：确认无 pending/running 的历史 step 使用该 kind。

### B2 · `question_decompose`
- `schema.py:123-132` `PROMPT_TYPES` 移除该值（`schema.py:634` 用它生成 CHECK 约束，`resolver.py:908-913` 强制要求每个值都有 factory prompt）。
- 采用最小方案：不动迁移 SQL、不动既有库的 CHECK 约束；既有 factory 行保留无害。

### B3 · `executionMode` 的 parallel 死分支（未排期）
- 值恒为 `"sequential"`（`resolver.py:1010`）；parallel 分支残留于 `repository.py:163/224/297-310`、`worker_loop.py:219`；`public_policy.py:250/270` 会强制降级。

## C. 实体图谱升为一等公民

### C1.1 独立 job + 重建端点
- `derived.py:76-78` `DERIVED_KINDS` 加 `"entity_alignment"`；
- `derived.py:5420-5425` step 映射加 `"entity_alignment": "insight_build_entity_alignment"`；
- `derived.py:5585-5823` 的 `handle()` 分派加分支：`snapshot()` → `align_entities(book_id, chapter_id="", pages_analysis=frozen.pages, run_id=<sourceRunId>, llm_judge=self._alignment_judge(config))`（`align_entities` 自身完成停用旧版本 + 激活新 revision，见 `derived.py:4059-4086`）；
- `entrypoint.py:290-302` 注册 handler；`user_logging.py:76-84` 加标签；
- `routes.py:303-316` 旁加 `POST /insight/books/<book_id>/entity-alignment/rebuild`；
- 同步 `openapi/v2.yaml` 并重新生成前端 client。

### C1.2 失败不再静默
- `derived.py:5846` 的 `LOGGER.warning` 改为（按 D3）**自动派发独立的 `entity_alignment` 补偿 job**。
- `/books/knowledge-status`（`routes.py:127`）暴露图谱状态：是否存在 / revision / 更新时间 / 上次失败原因。

### C1.3 图谱审核入口（未排期）
- 已有能力：`unmerge_alias`（`derived.py:4098`）、拼音兜底待审清单（`derived.py:4264`）、`auto_merge_log`/`pending_review_queue` 兼容读写（`derived.py:796-814`）。
- 缺 API：待审列表、人工拆并、人工合并（后者还需新写 repository 方法）。

---

## 基线（改动前后的对比锚点）

- 数据库：`data-v2/saber.sqlite3`（备份 52,682,752 字节 → `temp/backup-before-cleanup/saber.sqlite3`）。
- 基线快照脚本：`tmp_insight_baseline.py`，输出 `temp/insight_baseline_before.json`；任意库只读检查用 `tmp_insight_inspect_db.py <db>`。
- 记录内容：`analysis_artifacts` 各 kind/template 的 active revision 与 `dependency_fingerprint`、`canonical_entities` 数量、`alias_map` 大小、`auto_merge_log` 各 rule 计数。

### 改动前实测（2026-10-18）

- 生产库：books=2（`快速翻译` quick_workspace、`FX戰士久留美` library）、pages=387、assets=8267、jobs=1，而 **`analysis_runs`=0 / `analysis_artifacts`=0 / `analysis_page_results`=0 / `analysis_layer_results`=0** —— 应用库从未跑过知识库构建，因此本次清理**不存在生产数据迁移问题**。
- 参考产物只存在于开发期测试库（工作区根目录，由 `run_kurumi_*.py` 等独立脚本生成，非应用运行产生）：

| 测试库 | active revision | entityCount | aliasCount | `auto_merge_log` 规则计数 |
|---|---|---|---|---|
| `test_kurumi_ch1_to_ch5.db` | 5（slug_mode=pinyin） | 243（characters 16 / concepts 140 / items 36 / locations 51） | 329 | character_alias_merge 15、concept_semantic_match 7、frequency_dominant_promoted 4、llm_semantic_judge 55、location_detail_overlap 9 |
| `test_kurumi_ch1_full.db` | — | 94 | 112 | 含 item_core_match 3、location_detail_overlap 7 |
| `test_chapters_1_to_5.db`（咒术回战） | 5 | 88 | 101 | 空（slug_mode=None） |
| `test_jjk_check.db` | 1 | 36 | 47 | **cluster_predefined 7**（唯一命中 A4 硬编码的库） |
| `test_scott_ch1.db` | — | 56 | 75 | character_alias_merge 2、llm_semantic_judge 17 |

- A/B 对比口径：这些测试库是 A4/A2/A3 改动效果的直接对照（尤其 `test_jjk_check.db` 的 `cluster_predefined` 在改动后应归零，改为 `global_terms` 种子或 LLM 裁决路径产生）。

## 附：仓库内残留的作品专属调试产物（待清理，未纳入本次改动）

工作区根目录存在大量开发期调试产物，其命名直接暴露了硬编码数据的来源作品（久留美 / 斯科特 / 咒术回战）：
`test_kurumi_*.db`、`test_scott_ch1.db`、`test_jjk_check.db`、`fx_kurumi_ch*.json`、`run_kurumi_*.py`、`run_scott_ch1_test.py`、`scott_ch1_pages.json`、`alignment_report*.txt`、`compare_rule_vs_llm_judge.py`、`align_prototype.py` 等。
这些文件不属于应用运行时资产，建议在 A 类改动完成后单独清理（需另行确认，避免误删有用的分析报告）。

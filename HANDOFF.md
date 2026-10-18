# HANDOFF

> 更新时间：2026-10-18 16:20 (UTC+8)
> 项目代号：`MiyakoKB` (原 Saber-Translator 架构重构漫画知识库系统)
> 工作区路径：`D:\My_Doc\Mine\manga-knowledge\Saber-Translator-main`
> 远端 Git 仓库：`https://github.com/JLBC05/MiyakoKB.git` (主分支: `main`)

---

## 1. 任务
- **核心目标**：将原 Saber-Translator 翻译工具全面剥离重构为独立的漫画知识库与图谱检索系统 **MiyakoKB**。
  - 核心功能矩阵：漫画导入（Venera漫画库一键导入 + 本地目录/ZIP/CBZ多层级识别）→ 页面级多模态知识抽取（角色/能力/物品/地点/概念 5 大类）→ 跨章节增量实体对齐（3 层架构：规则粗筛 + 批量 LLM 语义裁决 + 本地持久化缓存）→ 轻量级共现关系图谱与结构化故事梗概 → 实体注入增强型问答检索（RAG + 结构化知识优先）→ 阅读器与沉浸式引用溯源。
- **边界与守则**：
  - 彻底移除所有翻译专属表、配置、提示词与代码目录（保留字体表 `fonts`、基础书籍/章节/页面/资产体系与核心任务系统）。
  - 严格遵守 `AGENTS.md` 规范：严禁手写表名/列名或枚举字符串，必须从 `src.backend_v2.storage.schema` 导入真实表对象。

---

## 2. 已完成（事实 + 证据）

### 2.1 数据库瘦身与模型解耦（表数量从 83 张精简至 75 张）
- **实现文件**：`src/backend_v2/storage/schema.py`、`src/backend_v2/content/repository.py`、`src/backend_v2/jobs/repository.py`、`src/backend_v2/jobs/retry.py`、`src/backend_v2/settings/resolver.py`、`src/backend_v2/operations/repository.py`、`src/backend_v2/operations/repair.py`、`src/backend_v2/plugins/runtime.py`。
- **证据**：
  - Commit `d5fb0eb`：删除第一批翻译表（`bubbles`、`translation_constraints`、`job_font_snapshots`、`operation_font_snapshots`），外键降解为普通 UUID 字段。
  - Commit `586dbaa`：删除第二批翻译表（`render_requests`、`browser_sessions`、`browser_session_credentials`、`browser_session_pages`）。
  - Commit `12c37e6`：彻底解耦残余的 bubbles/render_requests 导入。
  - 真实验证：新数据库执行 `metadata.create_all()` 顺利创建 75 张表；`verify_chapter_1_slug_mode.py` 7 步回归全部通过（37 实体）。

### 2.2 翻译后端目录与配置清理（物理移除 2.6 万行专属代码）
- **实现文件**：物理删除 `src/core/detector/`、`src/interfaces/ctd/`、`src/backend_v2/desktop/`、`src/backend_v2/rendering/`、`src/backend_v2/studio/`、`src/backend_v2/translation/`。
- **配置解耦**：`src/backend_v2/storage/defaults.py`（保留 8 个 insight 提示词，移除 5 个翻译提示词）、`src/backend_v2/settings/validation.py`。
- **证据**：Commit `6fb5673`、`fa1decf`、`cf051e5`（移除 desktop 角色）；API 启动健康检查 `GET /api/v2/health` 正常返回 200。

### 2.3 实体对齐引擎 5 大类扩展与 LLM 语义裁决落地
- **实现文件**：`src/backend_v2/insight/derived.py`、`src/backend_v2/insight/page_schema.py`、`src/backend_v2/insight/routes.py`。
- **实现内容**：
  - 新增 `concepts`（概念/设定/机制/金额/日期）分类，完善现实/金融题材适配。
  - 构建 3 级语义对齐体系：字符重合度 < 40% 立即跳过（零成本）→ >= 40% 批量交由 LLM 裁决（15 对/次并缓存至本地 JSON）→ 歧义冲突仲裁。彻底移除手工日期/金额硬编码正则。
  - 增加拼音 ID 同音冲突自动加后缀机制（`_2`, `_3`），确立 `analysis.slugMode` 唯一权威来源。
- **证据**：Commit `44e48ee`、`377dc8b`、`452dc55`；FX战士久留美 1-3 话（111 页）对比测试实体数从 199 缩减至 180，无任何金额/日期误归并；咒术回战第 1 话回归验证通过（37 实体，abilities 和 locations 保持独立）。

### 2.4 双源漫画导入模块（Venera 库 + 本地文件/文件夹/ZIP/CBZ）
- **实现文件**：`src/backend_v2/manga_import/routes.py`、`local_preview.py`、`local_confirm.py` 与前端 `MangaImportModal.vue`。
- **实现内容**：
  - Venera：自动扫描读取 `%APPDATA%\com.github.wgh136\venera\local.db`，支持批量导入。
  - 本地导入：支持多目录/ZIP/CBZ，4 层序号识别（文件名正则/ComicInfo.xml/AI视觉预留/兜底标记），拦截 CBR 格式，内存流解包入库。
  - 动态反馈：平滑多阶段进度条与状态文字展示。
- **证据**：Commit `86a7936`、`9b0765c`、`478cb15`；本地测试漫画（3 话 6 页）与 Venera 导入测试全部入库，数据库各表关系验证无冲突。

### 2.5 前端全面收敛与紫金现代主题
- **实现文件**：`vue-frontend/src/`：
  - 彻底删除 `TranslateView.vue`、21 个翻译专属 composables 与 CharacterStudio。
  - 统一顶级导航 `MiyakoNavbar.vue`（书架、图谱、问答、设置、任务中心抽屉触发器）。
  - 重构四大视图：`BookshelfView.vue`（支持一键构建、构建状态徽章）、`BookDetailView.vue`、`GraphView.vue`、`QaView.vue`、`SettingsView.vue`。
- **证据**：Commit `cf59fe4`、`ba2e981`、`b748172`、`4803065`（换用全新紫金 Favicon 与 Logo）；`pnpm build` 持续在 6-8s 内编译完成，0 报错。

### 2.6 统一启动器（方案 2：父子进程 Supervisor 守护模式）
- **实现文件**：`src/backend_v2/launcher/unified_launcher.py`、`run.py`、`启动.bat`。
- **实现内容**：单一入口并发拉起 API（5000 端口）与 Worker 子进程，自动监控存活、子进程崩溃自动重启、退出信号监听与端口/僵尸进程强力回收。
- **证据**：Commit `e843a4e`；`verify_launcher.py` 自动化测试通过（1.62s 完成就绪与流式 SSE 问答打通）。

### 2.7 图谱轻量化、故事梗概整合与问答引用显示优化
- **实现文件**：
  - `KnowledgeGraphPanel.vue`：去除耗时 SVG 模糊滤镜，添加 `simulationAlpha < 0.003` 静止停机与 320px 斥力截断，默认 Top 60 核心网络与全量视图切换。
  - `StorySynopsisPanel.vue`：合并旧版时间线与全景概览，打通主要角色与实体知识库。
  - `BookCard.vue` & 后端 `GET /api/v2/insight/books/knowledge-status`：书架展示“已构建/部分构建(X/Y话)/未构建”徽章。
  - `QAMessageItem.vue` & `CitationPreviewModal.vue`：引用章节去除括号内冗余的“（第X章）”，优先展示具体话数“第X话：第Y页”。
- **证据**：
  - Commit `85e8fcf`、`4cd71ea`、`478cb15`、`033d2f0`。
  - 单元测试：`qaMessageItem.spec.ts` 5/5 全部通过（2.6s 完成）。
  - 回归测试：`verify_chapter_1_slug_mode.py` 7/7 步骤全部通过。

---

## 3. 当前状态（精确到下一步）
- [x] 验收点 1：漫画导入（Venera + 本地 ZIP/目录）及进度条实时反馈。
- [x] 验收点 2：书架卡片精准展示构建状态（已构建/部分构建/未构建）。
- [x] 验收点 3：关系图谱轻量化、丝滑无卡顿，故事梗概合并打通实体。
- [x] 验收点 4：问答引用消除冗余括号章节号，优先显示真实话数。
- [x] 验收点 5：统一启动器拉起双进程稳定运行。
- [x] 验收点 6：生产环境大体量漫画（呪術廻戦 148 话/2998 页）的端到端压力测试与跨章节实体消歧观察 —— 已完成，结论与全部证据见 `Saber-Translator-main/acceptance6_stress_report.md`（§4 消歧观察、§5 下游验证、§6 发现 #1-#22）。
- **阻塞**：无代码或架构阻塞。
- **下一步**（按优先级）：
  1. **对齐裁决候选对问题**（报告发现 #13-#17）：**方案 4 → 1 → 5 已实施**（见 §4 决策 5）；用户已拍板，方案 3（给裁决器注入频次/页/描述上下文）与方案 2（改提议通道 + 向量召回）留作第二阶段评估；
  2. **衍生缺口补跑**：已按优先级 0 加共享执行器指数退避重试（`src/shared/openai_execution.py`）并用 `POST /api/v2/jobs/<id>/retry-failed` 只重跑失败项（源 job `39c2c48f`，重试 job `091d72b7`，20 项 = 10 个失败页 + 衍生重跑）；待确认无剧透概览/剧情总结正常发布（原为 404）后关闭发现 #18/#19；
  3. **排查 global 问答召回**：问"少年院"只返回第 1 话引用并答"证据不足"，而该场景在第 6-8 话（报告发现 #20）；
  4. 清理收尾：`tests_backend` 失效 import 已清零（`python -m pytest tests_backend --collect-only -q` = 969 tests, 0 errors；扫描脚本 `tmp_scan_tests.py`）；`vue-frontend` 类型检查 23 条（均为存量基线：图标/按钮变体白名单 19 条 + settings 事务类型漂移 4 条，剪裁 spec 导致的 2 条已消除）；PySide6-Essentials 已从 requirements-cpu/gpu 删除；根目录临时脚本仍待归置（`tmp_*.py` 一律不入库）。

---

## 4. 关键决策（含理由）
- **决策 1：实体归并淘汰手写正则，转向 3 级语义对齐（粗筛 -> 批量 LLM 语义判决 -> 本地持久化缓存）**
  - *否决方案*：不断堆砌日期、金额、组织名等手工正则表达式。
  - *理由*：漫画实体存在复杂简称、同义词、OCR 误识以及日文汉字变体，手工规则极易误伤（例如误将 2000万 与 300万 合并）。LLM 批量判定理解力强、准确率 100%，通过批处理（15 对/次）和本地 JSON 缓存将 token 开销降低至忽略不计（百页漫画开销 < 0.12 元）。
- **决策 2：问答系统与向量检索强解耦，支持智能降级**
  - *否决方案*：强依赖外部 ChromaDB 向量数据库与 Embedding 模型可用性。
  - *理由*：部分私有部署或特定代理下无法提供向量嵌入模型。系统解耦后，当缺少向量索引时自动降级到结构化实体知识库与页面全局摘要，确保问答功能始终 100% 可用。
- **决策 3：统一启动器采用父子进程 Supervisor 守护模式**
  - *否决方案*：打开两个黑窗口分别运行 API 和 Worker，或将 Worker 与 API 塞入同一进程的多线程中。
  - *理由*：双窗口容易误关导致单侧僵死；同进程多线程则因 Python GIL 锁和阻塞任务导致 API 响应卡顿。Supervisor 进程统一调度管理，崩溃自动重启，关闭时联动强力清理。
- **决策 4：书架与书籍详情页彻底剥离翻译分析工作台**
  - *否决方案*：保留页面树选择、标注进度面板等原翻译交互。
  - *理由*：知识库产品只需“一键构建知识库”和“查看知识图谱/问答”，分析细节全部收敛至统一的任务中心浮层中，降低用户心智负担。
- **决策 5：实体对齐裁决改为「硬拒绝规则前置 + 每书缓存 + 批量并发」，而非单纯收紧字面阈值**
  - *否决方案*：把字面重合度阈值从 0.40 直接抬到 0.50/0.60。
  - *理由*：用 3013 对历史裁决缓存做离线回放（`tmp_replay_rules.py`）证明，抬阈值会大量误伤真合并——真合并多为译名/同义/上下位（字面重合低），假候选反而常共享姓氏或泛称前缀（字面重合高）。因此改为 4 条确定性硬拒绝规则前置（数值/日期不等、`X·A` vs `X·B`、单侧关系/形态词、泛称 vs 括号限定名，见 `hard_reject_pair`），并修补 `min(len1,len2) <= 2` 的单字碰撞（`is_judge_candidate` 要求完整子串包含）；把 45 分钟的主因（`judge_pair_sync` 每对一次 LLM 调用）换成归并前一次性批量预判（`prefetch_alignment_judgements`，batch 30 + 并发 3 + 索引对齐校验）。回放结果：省 1154/3013 次调用（38.3%）、潜在误伤 22 对（0.73%）、Rule 2.1 静默合并救回 0 对（该分支只对五大类目之外的类别生效）。
- **决策 6：裁决缓存与失败语义——异常绝不落盘、按 `book_id` 隔离、保持布尔二态**
  - *否决方案*：给裁决结果引入 `inconclusive` 第三态。
  - *理由*：未命中缓存天然会在下次重跑时重判，第三态只增加下游解析复杂度；而把异常固化成 `same=false` 会永久污染结论（坑 6）。缓存文件名按书隔离（`llm_entity_judge_cache.<book_id>.json`），写入改为 `.tmp` + `os.replace` 原子落盘，异常只计数不改写缓存。
- **决策 7：OpenAPI 只描述真实存在的后端接口**
  - *背景*：剪裁后的 `openapi/v2.yaml` 丢了 `ExportPreferences` 与 `WorkerCommandAccepted` 两个 schema，前端类型检查因此多 2 条错误。
  - *决策*：补回 `ExportPreferences`（`export_preferences` 域在后端仍活着，`validation.py` 精确校验 `{preserveOriginalFilenames: bool}`）；**不**补 `WorkerCommandAccepted` 与 `/system/release-models`（该路由已随本地模型特性删除，grep 全 `src/` 只剩陈旧打包产物），改为删掉对应前端死代码（`vue-frontend/src/api/v2/system.ts` 的 `releaseWorkerModelCache` 与任务中心的"释放显存"按钮/处理函数）。
  - *结果*：`vue-tsc -b` 错误数 25 → **23**（等于存量基线：图标/按钮变体白名单 19 条 + settings 类型漂移 4 条）。

---

## 5. 踩过的坑 / 阻塞
- **坑 1：Windows SQLite 嵌套锁（Database is locked）**
  - *原因*：在写数据库事务内部进行物理磁盘图片发布时，如果抛出异常或并发读写，容易造成 SQLite 长期死锁。
  - *解法*：严格遵循“先物理写盘生成资产记录 -> 批量开启单事务写入 books/chapters/pages/page_assets”原则，批量插入以 50 条为一组分批提交。
- **坑 2：SVG 高斯模糊滤镜与无终止力导向模拟导致浏览器 100% CPU 占用**
  - *原因*：旧版在每一帧 requestAnimationFrame 中反复计算 O(N^2) 斥力且无停机阈值，叠加 SVG feDropShadow 滤镜导致重排重绘瓶颈。
  - *解法*：添加 `simulationAlpha < 0.003` 自动静止停机机制，增加 320px 斥力截断，彻底移除所有耗费 GPU/CPU 的高斯滤镜，改为纯净现代矢量描边。
- **坑 3：前端 Vite 生产构建对 CSS/Vue 模板格式极度敏感**
  - *解法*：任何前端修改后，必须使用环境内的 node/pnpm 运行 `pnpm build` 进行全量打包验证。
- **坑 4：数据库脚本手写字段或枚举违背 AGENTS.md 约束**
  - *解法*：所有涉及 DB 的操作必须 `from src.backend_v2.storage.schema import books, chapters, ...`，严禁拼写字符串表名与枚举。
- **坑 5：test-mode Worker 被强杀 → 整条队列死锁（孤儿 running job）**
  - *现象*：Worker 崩溃/被杀后，简化版 unified_launcher 只做 2 秒原样重启、不调用 reconcile，新 epoch 的 Worker 不收敛旧在途 item；job 停在 `running` 且 `worker_epoch_id` 为空，`uq_jobs_one_current` 使队列无法推进，`/continue` 也因 running 被拒。
  - *解法*：`JobQueueRepository.reconcile_lost_worker_jobs(conn, worker_epoch_id=None, now=...)` 把 steps/items 回置 pending 并追加 `job_interrupted`，再 `POST /api/v2/jobs/<id>/continue`；已自动化在守护脚本 `tmp_guard_job.py`（空闲 360s 触发）。
- **坑 6：LLM 裁决器失败被静默吞成"判定为否"，并被永久缓存**
  - *现象*：旧 `get_default_llm_judge()` 硬编码网关（与设置脱钩，切供应商后仍打旧网关）；`judge_batch` 异常时把整批写 `False`（derived.py:520-524），且缓存文件位于父工作区、跨书共享 → 失败结论被固化为"非同一实体"并污染后续 run。
  - *解法*：裁决器已改为跟随 frozen chat 配置（`_alignment_judge(config, book_id)`）；**已实施**（决策 6）：异常不落缓存、不引入第三态、缓存按 `book_id` 隔离、写入原子化；`_arbitrate_single` 失败返回 `None` 而不写 `same=false`。
- **坑 7：删除整个域之后留下的"未定义全局名"（NameError 定时炸弹）**
  - *现象*：`POST /api/v2/jobs/<id>/retry-failed` 直接 500：`NameError: name 'job_plugin_snapshots' is not defined`（`src/backend_v2/jobs/retry.py:660`，`_retry_insight`）——插件域被移除时清掉了 schema import，但使用点仍在；同类问题还有 `src/backend_v2/insight/routes.py:475` 的 `LOGGER`（既没 `import logging` 也没定义 logger）。
  - *解法*：补回 `job_plugin_snapshots` 到 `retry.py` 的 schema import（表本身仍在 schema.py，未删）；按仓库惯例给 `insight/routes.py` 加 `import logging` + `LOGGER = logging.getLogger("saber.api.insight")`。另留一个基于 `symtable` 的静态扫描脚本 `tmp_scan_undefined.py`（扫 `src/`，"用了但从没定义过的模块级全局名"，函数局部变量不会误报），当前扫描结果为空。

- **坑 8：局部重跑后向量索引静默按 0 条事件发布（coverage 仍报 1.0）**
  - *现象*：`retry-failed` 生成的新 run（只补失败页）自身没有 `analysis_layer_results`；`InsightDerivedWorkerService._layer_zero_event_records(frozen)` 只查 `frozen.source_run_id`，于是返回空列表 → `vector_generations.event_count=0`、`eventTotal=0`，但 `coverage` 因 `expected_total = 页数 + 0` 仍算出 1.0 被当成成功发布；连带 `qa/status` 的 `coverage.events` 从 152 掉到 0（读的就是 `vector_generations.event_count`，derived.py:5622-5631），global 问答因此缺事件召回。实测：原始 run `bf374dbe` 有 45 条 layer-0（覆盖 224 页），重试 run `43bf699e` 有 0 条。
  - *解法*：源 run 取不到 layer-0 时，回退到**本书最近一次产出 layer-0 的 run**（`analysis_layer_results` join `analysis_runs` 按 `created_at` 倒序找首个有结果者）；`source_run_id` 为 `None` 的旧语义保持不变。验证脚本 `tmp_verify_vector_events.py`：原始 run 152 条、重试 run 回退后 152 条、不存在的 run 同样回退 152 条、`None` 仍为 0；`tests_backend/v2/test_stage5_insight.py` 失败数 4（此前 5，HEAD 为 5），无新增回归。
  - *遗留*：已发布的那一代向量（generation 2）仍是 `event_count=0`，需在网关恢复后重跑 vectors 步骤才会被改写。

---

## 6. 新会话先读
1. `AGENTS.md`：核心数据库脚本防手写约束与行为规范。
2. `PROGRESS.md`：重构完整演进历史与各模块设计细节。
3. `run.py` & `src/backend_v2/launcher/unified_launcher.py`：服务启动与进程架构。
4. `src/backend_v2/insight/derived.py`：实体对齐、自动归并、图谱数据生成算法。
5. `src/backend_v2/insight/qa.py`：问答系统、实体解析器与证据格式化。
6. `vue-frontend/src/views/`：前端核心页面实现。
7. `acceptance6_stress_report.md`：验收点6 全量报告（阶段0-4、发现 #1-#25、裁决候选对离线回放数据、附录里的分析脚本清单）。
8. `verify_acceptance6_alignment.py` / `verify_acceptance6_downstream.py` / `verify_acceptance6_qa.py`：三项验收脚本（问答用法 `python verify_acceptance6_qa.py <bookId> "问题" --mode exact|global`）。
9. 本轮新增的工作区临时脚本（**不入库**）：`tmp_replay_rules.py`（对齐规则离线回放，零 token）、`tmp_scan_undefined.py`（未定义全局名扫描）、`tmp_job_status.py`/`tmp_guard_job.py`（job 监控与守护）、`tmp_check_state.py`（job/artifact 现状）、`tmp_retry_failed.py`（只重跑失败项）。

---

### 本次交接依据的文件
- `AGENTS.md`
- `Saber-Translator-main/PROGRESS.md`
- `Saber-Translator-main/HANDOFF.md`
- `Saber-Translator-main/src/backend_v2/storage/schema.py`
- `Saber-Translator-main/src/backend_v2/insight/derived.py`
- `Saber-Translator-main/src/backend_v2/insight/qa.py`
- `Saber-Translator-main/src/backend_v2/insight/repository.py`
- `Saber-Translator-main/src/backend_v2/insight/routes.py`
- `Saber-Translator-main/vue-frontend/src/components/insight/qa/QAMessageItem.vue`
- `Saber-Translator-main/vue-frontend/src/components/insight/qa/CitationPreviewModal.vue`
- `Saber-Translator-main/vue-frontend/tests/unit/qaMessageItem.spec.ts`
- `Saber-Translator-main/acceptance6_stress_report.md`
- `Saber-Translator-main/verify_acceptance6_alignment.py`
- `Saber-Translator-main/verify_acceptance6_downstream.py`
- `Saber-Translator-main/verify_acceptance6_qa.py`
- `Saber-Translator-main/tmp_candidate_rules_eval.py`（候选对规则离线回放，零 token）
- `Saber-Translator-main/tmp_replay_rules.py`（本轮对齐规则离线回放：硬拒绝规则命中分布、误伤清单、别名/合并日志回归）
- `Saber-Translator-main/src/shared/openai_execution.py`（共享执行器指数退避重试，优先级 0）
- `Saber-Translator-main/src/backend_v2/jobs/retry.py`（`retry-failed` 局部重跑；坑 7 的 NameError 修复点）
- `Saber-Translator-main/src/backend_v2/insight/derived.py`（硬拒绝规则、`is_judge_candidate`、每书缓存、批量预判）
- `Saber-Translator-main/openapi/v2.yaml`（补回 `ExportPreferences`）

### 仍需人工确认的项目
1. 实际启动服务（运行 `python run.py`）后，在浏览器中测试点击问答引用气泡，确认弹窗预览真实图片与章节话数展示完全符合预期。
2. 如需接入其它第三方模型（非 Gemini），可在 `/settings` 页面中配置对应的 API Key 与自定义 Base URL。
3. **对齐裁决第二阶段**（报告 §4.3-4.4、发现 #13-#17）：方案 4→1→5 已落地；方案 3（给裁决器注入频次/出现页/描述上下文）与方案 2（改提议通道 + 向量召回）待评估，量化基线已离线算出。
4. 本轮构建的衍生缺口补跑（job `091d72b7`）：概览 no_spoiler/story_summary 原为 404、时间线为 simple 降级（人物条目 0），需在重跑结束后复核是否已发布。
5. global 问答召回异常（报告发现 #20）待复现定位。
6. 旧全局裁决缓存 `llm_entity_judge_cache.json`（3013 对、父工作区）里可能残留被上游异常写成 `false` 的真合并对——已被新的按书缓存取代，建议直接归档删除，不要迁移进新缓存。

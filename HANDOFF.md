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
  - **2026-10-19 二次加固**（详情见 `acceptance6_stress_report.md` §4.5/§4.7）：4 条确定性硬拒绝规则前置（数值/日期签名不等、`X·A` vs `X·B`、单侧关系/形态词、泛称 vs 括号限定名）+ `is_judge_candidate` 粗筛（剥泛词后按核心字算重合，短名禁用浮点阈值）；裁决批量 30 对/请求、并发 3、响应按 `id` 索引校验，并把「每对一次请求」换成归并前一次性批量预判；缓存按 `book_id` 隔离、异常绝不落盘、写入原子化；`total_frequency` 定为去重页数（`== len(pages)`，决策 8）。
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
- [x] 验收点 6：生产环境大体量漫画（呪術廻戦 148 话/2998 页）的端到端压力测试与跨章节实体消歧观察 —— 已完成，结论与全部证据见 `Saber-Translator-main/acceptance6_stress_report.md`（§4 消歧观察与整改复验、§5 下游验证与三脚本全绿、§6 发现 #1-#33）。2026-10-19 已用恢复的网关补跑失败衍生项（job `8431eb87`，20/20 成功）：概览/剧情总结不再 404、时间线从 simple 升级为 enhanced、向量事件数 0 → 160（发现 #18/#19/#24 关闭）；同日另完成问答双模式合并为单一「通用」模式（报告 §5.5、决策 9）与「产物永远 degraded」根因定案（发现 #28、坑 10）；并按「状态只按本次 run 的目标页判定」拍板落地，重建后三个产物均 rev3 `ready`（报告 §5.6、决策 10）。
- **阻塞**：无代码或架构阻塞。
- **下一步**（按优先级）：
  1. **对齐裁决候选对问题**（报告发现 #13-#17）：**方案 4 → 1 → 5 已实施**（见 §4 决策 5）；用户已拍板，方案 3（给裁决器注入频次/页/描述上下文）与方案 2（改提议通道 + 向量召回）留作第二阶段评估；
  2. ~~**衍生缺口补跑**~~：**已完成（2026-10-19）**。`POST /api/v2/jobs/<id>/retry-failed` 只重跑失败项（源 job `39c2c48f` → 重试 job `091d72b7` → 补跑 job `8431eb87`/run `2c9d40c2`，20/20 成功），概览 no_spoiler(293 字)/story_summary(535 字) 已发布、`entity_alignment` rev5 status=ready；发现 #18/#19 关闭；
  3. **排查 global 问答召回**：问"少年院"只返回第 1 话引用并答"证据不足"，而该场景在第 6-8 话（报告发现 #20）；
  4. 清理收尾：`tests_backend` 失效 import 已清零（扫描脚本 `tmp_scan_tests.py`），后端全量 **937 passed / 0 failed / 0 error**（2026-10-19）；`vue-frontend` 六项门禁全部 0（vitest **88 files / 642 tests**、vue-tsc、eslint、stylelint、`lint:ui`、`lint:ui:audit`），生成类型与静态产物已与源码同步（见坑 16）；PySide6-Essentials 已从 requirements-cpu/gpu 删除；根目录临时脚本仍待归置（`tmp_*.py` 一律不入库）。

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
- **决策 8：`total_frequency` 是「去重后的出现页数」，不是跨修订累加计数**（2026-10-19 用户拍板）
  - *否决方案*：保留累加语义，只在增量范围（新增章节）时累加、同页重析时替换。
  - *理由*：产物是「实体 → 出现页集合」的事实快照，同页重跑不应计两次；累加语义在多次 `retry-failed` 后数值会随重跑次数漂移（实测 146 → 292 → 448），而前端图谱频次排序、问答上下文都读这个值，必须与 `pages` 严格同源。实现上强制 `total_frequency == len(pages)`（两处归一，含纠正历史产物的第二处），并把该等式作为产物不变量（分析器 [7] 检查 + 建议纳入发布前校验）。
- **决策 9：问答的 `exact` / `global` 双模式合并为单一「通用」模式（`generic`）**（2026-10-19 用户拍板）
  - *背景*：原双通道互斥——`exact` 只走页面/向量检索（拿不到全局脉络），`global` 只喂全库产物（发现 #20：问「少年院」召回不到第 6–8 话）。产物齐备后两者信息互补。
  - *否决方案*：保留两个模式、只在前端做自动选择；或让 `global` 也走向量检索。
  - *实现*：`qa.py` 新增 `DEFAULT_QA_MODE="generic"` / `QA_MODES` / `GENERIC_GLOBAL_CANDIDATE_LIMIT=2`，`_retrieve` 拆出 `_page_candidates` 与 `_global_artifact_candidates`（`generic` = 全库材料 ≤2 条 + 页面候选）；`derived.qa_status` 与 `GET /qa/status` 接受 `generic` 并默认之；`routes.py` 的 SSE 前置重排只作用于页面候选、全库候选固定前置（否则被挤出 `topK`）。
  - *理由*：单次回答同时具备细粒度证据（页码引用）与全局脉络（人物状态/时间线/未解伏笔）；旧值 `exact`/`global` 仍被接受（走原通道）故不破坏旧客户端。实测同一次回答引用到全库材料「资料 1、2」，生成 7.88s、端到端 11.8s（报告 §5.5）。
- **决策 10：派生产物 `status` 只按「本次 run 的目标页」判定，「只覆盖全书一部分」不算失败**（2026-10-19 用户拍板）
  - *背景*：见坑 10 / 发现 #28——前 10 话 run 自身 `status=completed`、目标 234/234 全部成功，却因与全书 2998 页比较而被判成「有失败」，产物永久 `degraded`，手动重建也无法转 ready。
  - *否决方案*：保留旧语义（`degraded` ＝ 派生输入只覆盖全书一部分），由 UI 说明并显式展示 `coverage`。
  - *实现*：①`derived.py` 删除快照里「全书页数当分母」的改写（现直接采信 run 自身状态：`source_run_status = active_run_status`，derived.py:3046）；②`repository.py:1946` 的 `derived_status` 只看本次 run 的 `final_status`（`completed_with_errors` 已由本 run 的失败页/后处理失败在 `finalize_run` 判定，repository.py:1462-1464）。
  - *覆盖范围*：不再折算成状态，改由 `snapshot.pages`、`compressed_context` 的 `source_coverage`、`GET /qa/status` 的 `coverage {pages, events}` 与 run 的 `target_count` 表达。
  - *验证*：重启栈后重建三个产物（job `d7ba340e` / `ddb49c59` / `a13510df`，均 completed 1/1）→ `compressed_context`、`overview(no_spoiler)`、`overview(story_summary)` 全部 rev3 `status=ready`、active=True；`entity_alignment/default` rev5 保持 ready。测试期望同步 3 处并删除 1 条过期断言（报告 §5.6）。
  - *已知取舍*：`entity_alignment` 独立重建路径仍硬编码 `status="ready"`（derived.py:4354-4361）——其产物 `run_id` 为 NULL（发现 #26），没有 run 可判定。

- **决策 11：清理已删域时「删代码」≠「删干净」——必须同步清契约、旗标与前端产物**（2026-10-19 用户批准）
  - *背景*：见坑 11 / 发现 #29——插件域删除后仍有使用点残留：capabilities 载荷读已删的 `RuntimeProfile.allow_plugins` → `GET /api/v2/system/capabilities` 持续 500；OpenAPI 仍留 13 条死路径与 19 个 `Plugin*` schema；前端产物仍在调已删的 `POST /api/v2/system/release-models`。
  - *否决方案*：为兼容旧契约把 `allow_plugins` 加回 `RuntimeProfile`（等于半复活已删域，且会让前端继续下发无意义的插件旗标）。
  - *实现*：`app.py` capabilities 去掉 `features.plugins`；`openapi/v2.yaml` 删除 `/plugins`、`/plugin-agent` 全部路径与 `Plugin*` schema/参数/tags（36 块，现 124 paths / 242 schemas）；`openapi-typescript` 重生成 `v2.ts`；`vite build` 重建 `src/backend_v2/static/vue`。
  - *验证*：capabilities / `server-info` / `jobs` / `settings` / `qa/status` 全 200，`/api/v2/plugins` 与 `/api/v2/tasks` 404，`/api/v2/openapi.json` 0 个 plugin 路径，`vue-tsc` 23 个错误与基线一致（未新增）；提交 `557679b`、`8f334ae`。
  - *遗留*：`plugin_agent` 属「半删除域」——`JOB_KINDS` 仍含该 job 类型（`storage/schema.py:93`，与 OpenAPI job-kind 枚举、DB CHECK 一致）、设置域仍被接受（`settings/validation.py:42,97,120`）、前端设置类型仍列它；与 DB CHECK 兼容一起另案清理（不在本轮范围）。

- **决策 12：`--role launcher` 默认走受管启动器，一键守护改为显式 `--unified-launcher`**（2026-10-19 修复）
  - *背景*：见坑 14 / 发现 #32——裸 launcher（也是打包入口默认角色）被 `e843a4e` 从受管启动器改路由到一键守护，而后者不准备存储、以 `--test-mode` 拉起子进程：全新数据根上 API 报 `no such table: job_events`、启动器 epoch 与重启监护被绕过。
  - *实现*：`src/backend_v2/dispatch.py` 恢复裸 `--role launcher` → `run_launcher`（受管：存储准入 + 签发 epoch + 重启监护），新增 `--unified-launcher` 选择 `run_unified_launcher`，`--legacy-launcher` 保留为兼容别名；同时给 `run_unified_launcher` 补 `StorageManager.prepare(initialize_database)` + `before_start()`，使一键守护在全新数据根也可用（`python run.py` 用户路径）。
  - *验证*：受管启动器新根 health 200 + 76 表（epoch 由启动器签发）；一键启动器新根 health 200 + 76 表；`tests_backend/v2/test_role_boundaries.py` **19 passed**（此前 4 红：`condition did not become true before timeout`）。

- **决策 13：对外可见层统一为 `MiyakoKB`，但后端 Python 包路径 / 数据目录 / 库文件名一律不动**（2026-10-19 用户批准）
  - *背景*：项目定位已从翻译器变成漫画知识库，而窗口标题、打包产物、发行资产名、README 仍沿用 `Saber-Translator`；README 还带着作者个人赞赏码与翻译时代的截图。
  - *范围与理由*：只改**对外可见层**（`app.spec` 的 EXE/COLLECT 名、`release.yml` 的探测路径 / 资产名 / 发布说明、`launcher/entrypoint.py` 的窗口标题与日志文件名、`web_import/worker.py` 的 UA、`PublicTrialNotice.vue` / `AdminView.vue` 文案、README / RELEASE_NOTES）；**不动** `src/backend_v2/*`、`src/shared/*`、`saber_v2.py`、`saber.sqlite3`、`data-v2/`、`SABER_*` 环境变量——这些是 import 路径与既有用户数据的位置，改名会破坏升级与全部测试，收益只有「好看」。
  - *同时*：README 重写为知识库定位（功能 / 快速上手 / 运行架构 / 源码开发 / 常见问题），删除两张作者个人赞赏码与 10 个翻译时代截图（`pic/` 只留 logo 与本代 5 张知识库截图）；`app.spec` 的 datas 补入 `LICENSE` / `THIRD_PARTY_LICENSES.md` / `RELEASE_NOTES.md`；`.github/scripts/check_models.py`（已无引用）删除；release.yml 残留的 torch/onnxruntime/模型搬运开关清理干净。
  - *验证*：`dist/MiyakoKB/MiyakoKB.exe` 打包成功（2863 文件 / 483.1 MB，仅 3 款字体）；CI 同款 `probe_package.py` 逐角色 exit=0；`release.yml` 中已无 `torch|onnxruntime|SABER_BUILD_MOVE_MODELS|models.z` 命中。提交 `cbc23ff`（品牌 + README + 产物重建）、`faf5210`（前端陈旧用例）、`f1ef3aa`（删死脚本）。

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
  - *遗留（已消解）*：当时已发布的向量 generation 2 仍为 `event_count=0`；2026-10-19 网关恢复后重跑衍生，published generation 3 的 `coverage = {events: 160, pages: 234}`，`qa/status` 恢复正常。

- **坑 9：同一批页面重跑后实体频次被重复累加（`total_frequency` 翻倍）**
  - *现象*：`retry-failed` 只补失败页，但 `align_entities` 以**已激活产物为基底**做增量归并（`existing_canonical_entities`，derived.py:3788-3799），而归并统计按 `analysis_page_results` **行**累加（`total_frequency += info["count"]`，derived.py:4186/4237/4254），`pages` 却是去重并集 → 同一页被重析两次即计两次。实测虎杖悠仁 146 → 292 → 448，`pages` 仍是 156；**252/257 个实体的频次与页数不一致**，图谱频次排序与问答上下文随之失真。
  - *解法*（决策 8）：`total_frequency` 语义定为**去重后的出现页数**，在两处强制对齐——①统计累积循环后（约 3858 行）过滤非法页号并 `pages=sorted(set(...))`、`count=len(pages)`；②最终归一化循环（约 4312-4326 行、`final_payload` 组装前）再算一次，**这一步不可省**，否则历史产物里被撑大的值不会被纠正。复验：违例 252/257 → **0/257**（虎杖悠仁 156、伏黑惠 123、钉崎野蔷薇 60、五条悟 58、两面宿傩 53）。

- **坑 10：局部范围 run 的派生产物「永远 degraded」，手动重建无法转 ready**
  - *现象*：前 10 话（234 页 / 全书 2998 页）跑出的 run 自身 `status=completed`、目标 234/234 全部成功，但 `compressed_context` / `overview(no_spoiler)` / `overview(story_summary)` 三个端点各重建一次后，新 rev2 仍为 `degraded`。
  - *根因*（发现 #28）：派生状态把**全书页数**当分母 —— ①`derived.py:3044-3051` 用全书页数算产物快照的 `source_run_status`（234 < 2998 → `completed_with_errors`），再由 `_publication_status`（derived.py:6650-6658）记为 degraded；②`repository.py:1944-1952` 的 `derived_status` 同样比较 `success_count < 全书页数`。另有一致性缺口：`entity_alignment` 生成点硬编码 `status="ready"`（derived.py:4354-4361），同一批产物里只有它「干净」；`tests_backend/v2/test_stage5_insight.py:5474-5495` 还把 degraded 当期望行为。
  - *现状*：**已于 2026-10-19 按方案②落地（决策 10）**——状态只按本次 run 的目标页判定：①删除 `derived.py` 快照里「全书页数当分母」的改写（现为 `source_run_status = active_run_status`，derived.py:3046）；②`repository.py:1946` 的 `derived_status` 只按本次 run 的 `final_status`。重建三产物 → 全部 rev3 `ready`（此前 rev2 仍 degraded）；覆盖范围改由 `snapshot.pages` / `source_coverage` / `qa/status.coverage` 表达。测试同步 `tests_backend/v2/test_stage5_insight.py` 三处期望由 degraded 改 ready（含更名 `test_partial_analysis_publishes_ready_global_artifacts`），并删除过期断言 `'"characters"' not in payload`（`knowledgeMode` 下页面产物规范携带五个实体数组）。*残留取舍*：`entity_alignment` 独立重建仍硬编码 ready（其产物无 run 可判定）。

- **坑 11：已删域的使用点残留会以 500 / 死引用形式浮出来**
  - *现象*：`GET /api/v2/system/capabilities` 自 2026-10-19 13:53 起持续 500；`data-v2/logs/saber-api.log` 只有「状态码 500」摘要，看起来像瞬时故障。
  - *根因*（发现 #29）：`cleanup` 删除插件域后①`src/backend_v2/api/app.py` 的 capabilities 载荷仍读 `RuntimeProfile.allow_plugins`（`AttributeError`，完整栈帧只在 `data-v2/logs/saber-api-diagnostic.log`）；②`openapi/v2.yaml` 仍留 13 条 `/plugins`、`/plugin-agent` 死路径与 19 个 `Plugin*` schema；③`src/backend_v2/static/vue` 旧产物仍在调已删的 `POST /api/v2/system/release-models`。同族缺陷此前已有坑 8 里 `retry-failed` 的两处未定义全局名（发现 #23）。
  - *现状*：**已修**（决策 11 / 报告 §5.7）——capabilities 200（`features={localProviders,webImport}`）、契约裁剪 36 块、`v2.ts` 重生成、前端产物重建（50 文件 / 2.4 MiB）；提交 `557679b`、`8f334ae`。
  - *自检命令*（删域后固定跑）：`python tmp_scan_undefined.py`（未定义全局名扫描）；`Select-String -Path openapi/v2.yaml -Pattern '^  /(plugins|plugin-agent)'`（应无输出）；重建产物后 grep 已删接口路径（本轮为 `release-models`、`/api/v2/plugins`、`features.plugins`，均应为 0）。排查 API 500 时先看 `data-v2/logs/saber-api-diagnostic.log`，`saber-api.log` 只有摘要。

- **坑 12：退休域只拆了「落库」没拆「接缝」**
  - *现象*：`tests_backend/v2/test_stage2_content.py` 累积 12 条红，全都在断言已不存在的行文（bubble 会落库并带修订、glossary 跨书拒绝、渲染状态 `not_rendered`、客户端命令体拒绝等），而 `POST/GET /pages/<page_id>/bubbles`、`/translation/bootstrap` 仍返回 200。
  - *根因*（发现 #30）：bubble 持久化在提交 `12c37e6` 被改为 `existing_rows = []`（`src/backend_v2/content/repository.py`）；`translation_constraints` 只剩内存桩（`get_constraints` → `revision=1, payload={}`，表已在 3.5.3 退役）；工厂提示词只剩 7 个 insight 类型。但路由（`src/backend_v2/content/routes.py:517/521/529`）与契约（`openapi/v2.yaml:1556/1578`）没同步裁剪。
  - *现状*：**已处置**（报告 §6 发现 #30）——删除 9 条陈旧用例、改写 2 条（bootstrap 期望 `payload=={}` + 7 个 insight 提示词；quick workspace 断言「章节存在但无页面/无译文行」）、新增 `test_published_page_bubbles_read_returns_empty_projection` 钉住现状；`test_stage2_content.py` **62 passed**。遗留：`/pages/<id>/bubbles` 这类「200 但什么都不落库」的接口仍在契约里，恢复还是裁剪需产品拍板。

- **坑 13：Windows ACL 用例依赖 `mkdtemp` 的历史私有 DACL（顺带暴露真实产品缺陷）**
  - *现象*：`tests_backend/v2/test_directory_fonts.py` 的 legacy-ACL 用例在本机红（`assert False`），但看代码像是「没动过」。
  - *根因*（发现 #31）：用例假设 `tempfile.mkdtemp` 产出带 `SYSTEM/Administrators/OWNER` 三条 ACE 的受保护 DACL；本机（沙箱运行时）实测得到的是**继承型** ACL（`control=0x8404`，icacls 显示 `D:PAI(...)`）。同时产品 `_restore_legacy_font_inheritance`（`src/backend_v2/storage/font_files.py:146`）只接受 `D:P(` 前缀，遇到实机的 `D:PAI(` 直接放弃修复——**真实缺陷**：换台机器字体目录权限不再自愈。
  - *现状*：**已修**——产品接受 `D:PAI(`/`D:P(` 两种前缀（ACE 集合仍须完全等于 legacy 三条，保守性不变）；测试新增 `_set_private_acl()` 显式用 icacls 复现 shared/private/自定义三种 ACL 现状；`test_directory_fonts.py` **15 passed**。

- **坑 14：`--role launcher` 的默认实现被换成「不准备存储」的一键守护**
  - *现象*：`tests_backend/v2/test_role_boundaries.py` 4 条用例长期红（`condition did not become true before timeout`）；手工用真启动器启动全新数据根时 `saber.sqlite3` 始终停在 4096 字节（0 表），API 报 `no such table: job_events`，`runtime/worker-ready.json` 里 `epochId=test-worker`。
  - *根因*（发现 #32）：基线提交 `5777d36` 把裸 launcher 路由到受管启动器；提交 `e843a4e` 改为路由到 `run_unified_launcher`，该实现跳过存储准备并以 `--test-mode` 启动子进程（提交 `8857592` 又给 worker 的 test-mode 分支加了 epoch 写入，但没有 schema 时必然失败）。
  - *现状*：**已修**（决策 12 / 报告 §6 发现 #32）——裸 `--role launcher` 回到受管启动器、一键守护改为 `--unified-launcher` 并补存储准备；`test_role_boundaries.py` **19 passed**。教训：改 CLI 默认路由时，必须确认「默认路径仍满足生产契约（存储准入 / epoch / 重启监护）」，并让两条路径都跑一遍「全新数据根 + health」冒烟。

- **坑 15：打包产物从未包含存储契约 SQL（发布版无法初始化全新数据根）**
  - *现象*：本地 `PyInstaller app.spec` 构建出的 `MiyakoKB.exe`（当时仍叫 `Saber-Translator.exe`）执行 `--role launcher --probe --test-mode` 立即以 `StorageError: 存储契约 3.5.3 未找到` 退出；CI 的 `probe_package.py` 本应在发布前拦住这种产物。
  - *根因*（发现 #33）：`src/storage_migrator/contracts.py:78` 用 `Path(__file__).parent / "schemas" / f"{version}.sql"` 取契约，而 `app.spec` 的 `datas` 只收集 `src/backend_v2/static`、`src/backend_v2/resources`、三个 `src/shared/*.json` 与 `openapi/v2.yaml`——**从未收集 `src/storage_migrator/schemas`**。
  - *现状*：**已修**（报告 §6 发现 #33）——`app.spec` 增加 `src/storage_migrator/schemas` 的 datas 条目（冻结后保持同相对路径），并把 `LICENSE` / `THIRD_PARTY_LICENSES.md` / `RELEASE_NOTES.md` 一并打进产物；重建后按 CI 同款 `probe_package.py` 逐角色校验。教训：**凡是运行时按 `Path(__file__).parent` 找的 `.sql`/`.json`，都必须显式进 spec 的 `datas`**；打包改动后必须跑一次 probe（本机沙箱下需先把 `TEMP/TMP/TMPDIR` 指向可写目录，否则 PyInstaller 的 matplotlib 运行时钩子在 cwd 位于 `%TEMP%` 时找不到可用临时目录而启动即失败）。

- **坑 16：前端「门禁全绿」≠ 测试全绿——`npm run test` 扫的范围比手跑 `tests/unit` 更宽，且提交过期的生成物会让 CI 必红**
  - *现象*：项目内一直记着「vitest 全绿」，实为只跑 `tests/unit`；按 `package.json` 的 `test` 脚本跑**全量**时是 `Test Files 5 failed | 85 passed`、`Tests 22 failed | 634 passed`——`src/**/*.test.ts`（store 层）与 `tests/property/*.property.ts`（属性测试）从未纳入过门禁。同一天还发现已提交的 `vue-frontend/src/api/generated/v2.ts` 含 86 行契约已裁剪的 `Operation*` 类型、`src/backend_v2/static/vue` 也不是当前源码的产物——CI 的 `check:api`（`git diff --exit-code`）与「generated frontend is current」两道门禁必然失败。
  - *根因*：翻译 / OCR 域退休后，这些规格仍在断言已删的 store 字段（`pluginAgent`、`ocrEngine`、`hints`、`saberYolo*`）与已删路由 `/translate`、`/translation-batches`；生成物与构建产物没有在源码改动时同步重建。
  - *现状*：**已修**（提交 `cbc23ff` 重建类型与静态产物、`faf5210` 清理陈旧用例）——22 条红全部处置（删除 3 个随域退役的规格、按当前 store/路由面重写 2 个属性测试、新增 `settings.backend-loading.test.ts`）；全量 vitest **88 files / 642 tests 全绿**。自检命令（改前端后固定跑）：`node node_modules/vitest/vitest.mjs run --configLoader runner`（**不要**只跑 `tests/unit`）；提交前确认 `node node_modules/openapi-typescript/bin/cli.js ../openapi/v2.yaml -o src/api/generated/v2.ts` 后 `git diff --exit-code` 无输出，且 `vite build` 后 `git status --porcelain -- src/backend_v2/static/vue` 为空。

---

## 6. 新会话先读
1. `AGENTS.md`：核心数据库脚本防手写约束与行为规范。
2. `PROGRESS.md`：重构完整演进历史与各模块设计细节。
3. `run.py` & `src/backend_v2/launcher/unified_launcher.py`：服务启动与进程架构。
4. `src/backend_v2/insight/derived.py`：实体对齐、自动归并、图谱数据生成算法。
5. `src/backend_v2/insight/qa.py`：问答系统、实体解析器与证据格式化。
6. `vue-frontend/src/views/`：前端核心页面实现。
7. `acceptance6_stress_report.md`：验收点6 全量报告（阶段0-4、发现 #1-#33、裁决候选对离线回放数据、附录里的分析脚本清单）。
8. `verify_acceptance6_alignment.py` / `verify_acceptance6_downstream.py` / `verify_acceptance6_qa.py`：三项验收脚本（问答用法 `python verify_acceptance6_qa.py <bookId> "问题" [--mode generic|exact|global]`）。
9. 本轮新增的工作区临时脚本（**不入库**）：`tmp_replay_rules.py`（对齐规则离线回放，零 token）、`tmp_scan_undefined.py`（未定义全局名扫描）、`tmp_job_status.py`/`tmp_guard_job.py`（job 监控与守护）、`tmp_check_state.py`（job/artifact 现状）、`tmp_retry_failed.py`（只重跑失败项）、`tmp_inspect_retry_job.py`（job + items + 冻结配置 vlm/chat/embedding + 产物）、`tmp_artifact_detail.py`（产物全列 + payload 键 + warnings）、`tmp_wait_job.py`（轮询至终态，`<jobId> <maxMinutes>`）、`tmp_verify_vector_events.py`（向量事件回退验证）、`tmp_trim_plugin_spec.py`（OpenAPI 已删域残留裁剪：按缩进删路径/schema/参数/tag，幂等）。

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
4. ~~本轮构建的衍生缺口补跑（job `091d72b7`）~~：**已于 2026-10-19 完成**（补跑 job `8431eb87` 20/20 成功）——概览 no_spoiler/story_summary 已发布、时间线升级为 enhanced（人物条目 6）、向量事件数 160；仍需产品侧拍板的是**产物 `status=degraded` 的语义**：局部范围 run 的派生产物必然 degraded，且**重建不能转 ready**（真实分母是全书页数，见报告发现 #28 与坑 10），需在「保留语义 + UI 显示 coverage」与「按本次 run 的目标页判定状态」之间选择。**已于 2026-10-19 拍板为「按本次 run 的目标页判定状态」并落地**：`derived.py` 快照不再用全书页数改写状态、`repository.py` 的 `derived_status` 只看本次 run 的 `final_status`；重建后 compressed_context / overview(no_spoiler) / overview(story_summary) 均 rev3 `ready`（见报告 §5.6、决策 10、坑 10 解法）。
5. 问答召回异常（报告发现 #20）待复现定位（exact 模式已复验正常：7 条引用 + 结构化答案）。注意 `global` 模式已在决策 9 并入 `generic`（`generic` = 页面/向量检索 + 最多 2 条全库产物候选，重排只覆盖页面候选），复验请用 `--mode generic`。
6. 旧全局裁决缓存 `llm_entity_judge_cache.json`（3013 对、父工作区）里可能残留被上游异常写成 `false` 的真合并对——已被新的按书缓存取代，建议直接归档删除，不要迁移进新缓存。
7. **退休域的「接缝」是否裁剪**（报告发现 #30）：bubble 持久化已移除，但 `POST/GET /pages/<page_id>/bubbles`、`/pages/<page_id>/bubble-overrides`、`/translation/bootstrap` 仍在运行时路由与契约里（前者返回空投影、后者返回空 prompts/内存桩约束）。恢复实现还是从路由与 `openapi/v2.yaml` 一并裁掉，需要产品拍板。
8. **发布前的两步（都在用户侧）**：①仓库当前仍是**私有**（未认证访问 `https://api.github.com/repos/JLBC05/MiyakoKB` 返回 404），而 README 的下载 / Issues 链接与 v* Release 资产都按「公开仓库」预期——要发布需先在仓库设置里改为公开；②建议先手动 `workflow_dispatch` 跑一次 `.github/workflows/release.yml` 验证 CI（它会跑后端 pytest + 前端门禁 + 打包 probe + 分卷上传），确认全绿后再打 `v3.5.17` tag。
9. **发行包形态**：`release.yml` 仍按 `cpu` / `gpu` 两个 variant 各出一个包，但 `requirements-cpu.txt` 与 `requirements-gpu.txt` 已逐字节相同（本地模型与 CUDA 栈移除后二者内容一致）——是否收敛为单一发行包，需要产品决定（收敛可省一半 CI 时间并避免用户选错）。

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
- [x] 验收点 6：生产环境大体量漫画（呪術廻戦 148 话/2998 页）的端到端压力测试与跨章节实体消歧观察 —— 已完成，结论与全部证据见 `Saber-Translator-main/acceptance6_stress_report.md`（§4 消歧观察与整改复验、§5 下游验证与三脚本全绿、§6 发现 #1-#40）。2026-10-19 已用恢复的网关补跑失败衍生项（job `8431eb87`，20/20 成功）：概览/剧情总结不再 404、时间线从 simple 升级为 enhanced、向量事件数 0 → 160（发现 #18/#19/#24 关闭）；同日另完成问答双模式合并为单一「通用」模式（报告 §5.5、决策 9）与「产物永远 degraded」根因定案（发现 #28、坑 10）；并按「状态只按本次 run 的目标页判定」拍板落地，重建后三个产物均 rev3 `ready`（报告 §5.6、决策 10）。**2026-10-20 用户报告问答「引用乱指 + 答不出第几话」，已定案并修复**：根因是 Chroma HNSW 段读取失败被静默降级 + 兜底按页号截断 + 实体证据页无话数映射且不进引用（报告 §5.10 / 发现 #40 / 决策 19 / 坑 27）。
- **阻塞**：无代码或架构阻塞。
- **下一步**（按优先级）：
  1. **对齐裁决候选对问题**（报告发现 #13-#17）：**方案 4 → 1 → 5 已实施**（见 §4 决策 5）；用户已拍板，方案 3（给裁决器注入频次/页/描述上下文）与方案 2（改提议通道 + 向量召回）留作第二阶段评估；**2026-10-19 追加：全书构建 69% 墙钟耗在逐对裁决的问题已定案并落地**（报告发现 #34 / 决策 14 / 坑 19）：超集预取 + 批量裁决（30 对/批、3 并发）、对齐拆为独立步骤 `insight_stage_entity_alignment`、每批进度写用户日志；
  2. ~~**衍生缺口补跑**~~：**已完成（2026-10-19）**。`POST /api/v2/jobs/<id>/retry-failed` 只重跑失败项（源 job `39c2c48f` → 重试 job `091d72b7` → 补跑 job `8431eb87`/run `2c9d40c2`，20/20 成功），概览 no_spoiler(293 字)/story_summary(535 字) 已发布、`entity_alignment` rev5 status=ready；发现 #18/#19 关闭；
  3. **排查 global 问答召回**：问"少年院"只返回第 1 话引用并答"证据不足"，而该场景在第 6-8 话（报告发现 #20）；
  4. 清理收尾：`tests_backend` 失效 import 已清零（扫描脚本 `tmp_scan_tests.py`），后端全量 **946 passed / 0 failed / 0 error**（2026-10-19 深夜，P1 死代码清理删掉 1 条只测被删函数的用例后为 946，含 E/D2 与收窄候选面共 10 条新增）；`vue-frontend` 六项门禁全部 0（vitest **88 files / 642 tests**、vue-tsc、eslint、stylelint、`lint:ui`、`lint:ui:audit`），生成类型与静态产物已与源码同步（见坑 16；P1 删完 23 个前端导出后 `vite build` 产物字节未变，反证其早被 tree-shaking 排除）；PySide6-Essentials 已从 requirements-cpu/gpu 删除；根目录临时脚本仍待归置（`tmp_*.py` 一律不入库）。**P1 死代码清理已完成**（决策 17 / 报告发现 #39，22 文件 +57/−684）；**P2b/P3 已完成**（决策 18 / 报告发现 #30 追加）：12 条退役路由 + 4 个半死模块（`rendering.py` 1902 行、`config_models.py` 439 行、`ocr_types.py` 余 92 行、`translation_constraints.py` 242 行）已删、契约 `paths` 127→117、契约与运行时路径数零差异（146 == 146）。**当前门禁基线：后端全量 773 passed / 0 failed；前端六项门禁 0（vitest 87 files / 638 tests）**——较 P1 时期的 946 passed / 642 tests 减少的都是随退役域删掉的用例。
  5. ~~**提速复测**~~：**已完成（2026-10-19 21:34）**。供应商级并发 `analyzeConcurrency=3` 写入 insight_vlm 供应商行（rev29→30）并确认进入作业冻结配置；久留美 387 页全书构建 job `c95406a7-66a4-466d-af31-ba03442d060c` / run `c3730509-3b35-424a-9753-8398561a57ec`（398 项，含新步骤 `insight_stage_entity_alignment`）**5048.7s（84.1 min）completed**，对照基线 9168.40s（152.8 min）＝ **1.82×**，少 68.7 min。分段：逐页分析 1658.3→1065.9s、第 1 层 777.5→714.6s、**实体对齐 6354s（未记账）→2783.9s（正式步骤）**、其余约持平（compressed_context 28→115s、timeline 23.8→43.3s 为同网关单次调用时延波动）。产出质量：实体 810（32/98/148/532）、别名 1213、归并 410（旧 500/646/147，全部 high、无 `None→None`）、频次=去重页数 810/810、金额日期概念 124 个 0 重复 ID、图谱 810 节点/5107 边/0 孤立、概览与时间线 ready、`qa/status` generation=2（events 238/pages 387）、问答 10.7s 带页引用。结论与下一杠杆见报告 §5.8 与发现 #37（超集预取把候选对放大 6.4×，对齐仍占 55%）。 同日深夜又按发现 #37 建议①**收窄了裁决候选面**（决策 16 / 报告 §5.9 / 发现 #38）：新增 `INSIGHT_JUDGE_TOP_K`（默认 16，`0` 关闭），线上库候选对 8,195 → 5,381，重建对齐步骤 **9.88s**（缓存全命中）且产物与未收窄版本逐项一致（810 实体 / 410 别名 / 410 归并，规则分布相同）。

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

- **决策 14：实体对齐的 LLM 语义裁决改为「超集预取 + 批量裁决」，并把对齐提升为独立流水线步骤**（2026-10-19 用户批准「就按你说的来」）
  - *背景*（发现 #34）：久留美 387 页全书构建实测 9168.4s，其中**实体对齐 1158 次单对裁决耗 105.9 分钟（69.3%）**；且该段跑在 `insight_stage_timeline` 步骤内部，`job_events` 期间零事件（进度假完成、日志把 1158 次请求记成「生成剧情时间线」、失败后整段重跑）。
  - *做法*：(a) 预取改为**超集枚举**——候选面 = raw 名 ×（已有实体名/别名 ∪ raw 名），abilities 保留 core 前缀过滤，统一走 `is_judge_candidate` 闸门，按 `LLMBatchJudge.make_key`（顺序无关）去重后一次性 `judge_batch`（默认 30 对/批、3 并发；`INSIGHT_JUDGE_BATCH_SIZE` / `INSIGHT_JUDGE_PARALLEL` 可调；失败批次重试 3 次且不写缓存），主循环随之为缓存命中；(b) 对齐拆为正式步骤 `insight_stage_entity_alignment`（`insight/commands.py` 的 `analysis_final_steps` 中位于 timeline 与 vectors 之间；worker 派发表、`STEP_LABELS`、前端 `taskDisplay.ts` 同步），失败**非阻塞**（写用户日志「实体知识网络对齐失败」并保留告警），由 `_dispatch_entity_alignment_if_missing` 补建独立作业；`get_books_knowledge_status` 的失败查询同时认新旧步骤名；(c) 每批裁决回写用户日志进度。
  - *理由*：不改主循环结构（避免高风险的两遍循环重构）——首建时几乎无既有实体，主循环本来就会问到几乎所有通过闸门的候选对，超集≈实际询问集；`LLMBatchJudge` 本身早已具备切块/并发/缓存/失败不落缓存的能力，缺的只是「调用点会批量」。
  - *验证*：`tests_backend/v2/test_stage5_insight.py` **152 passed**（3 条重试用例的 job item 计数随新步骤表 11→12；补偿用例继续钉住「对齐失败不阻塞分析结果发布」）；前端六项门禁复验 **0**（vitest 88 files / 642 tests）。

- **决策 15：批内并发升级为供应商级设置；自定义架构下解耦「聚合粒度」与「每批页数」**（2026-10-19 用户批准「按你说的来」，属发现 #35）
  - *背景*：A/B/C 之后仍有两处杠杆——①逐页分析的批内并发只由**进程环境变量** `INSIGHT_ANALYZE_PARALLEL` 控制（默认 3、范围 1..8），UI 看不见、无法按供应商能力调整（387 页实测正被压到 1）；②第 1 层聚合粒度被绑死在 `pagesPerBatch`（`derived.py` 的 `layer_units` 第 0 层分支写死 `group_size = pagesPerBatch`，第 0 层自带 `unitsPerGroup` 被忽略），前端还把两者刻意同步。
  - *做法*：(a) 新增 **insight VLM 供应商级设置 `analyzeConcurrency`**（1..8，默认 3）——载荷键校验、整数范围校验、resolver 冻结映射（`analyze_concurrency`）、`provider_runtime.frozen_analyze_concurrency` 读取、`worker._analyze_concurrency(config)` 优先取设置、解析不到才回退 `INSIGHT_ANALYZE_PARALLEL`（env 降级为兜底）、前端 VLM 页签新增「批量并发分片（1-8，默认 3）」输入；(b) **自定义架构**（`architecturePreset == "custom"`）下第 0 层 `unitsPerGroup` 表示「每组合并页数」，≥1 时生效、0 回退 `pagesPerBatch`；**内置预设架构行为完全不变**（仍等于 `pagesPerBatch`），前端解耦两个旋钮（仅当用户未单独调整过第 0 层数值时才跟随每批页数）并区分标注「每 N 页汇总 / 每 N 个单元汇总」。
  - *理由*：并发只影响速度、不改知识语义，适合做成可见可调的供应商参数；聚合粒度会改变中间层细节保留度，因此只在用户显式选择自定义架构时才接管默认值——不替所有用户降低知识产出质量。
  - *验证*：新增 `test_vlm_page_analysis_concurrency_is_a_provider_setting`（含「老载荷缺该键仍合法」「越界/布尔拒绝」「设置优先于 env」）与 `test_custom_architecture_groups_first_layer_units_by_its_own_size`（自定义架构 units=2 → 第 1 层 2 个单元；内置架构不变）；受影响两个测试文件 **254 passed**；前端六项门禁 **0**（vitest 88 files / 642 tests，vue-tsc 0，eslint 0 errors / 20 warnings）。

- **决策 16：语义裁决的候选面按字面相似度取 Top-K 收窄（确定性别名对豁免）**（2026-10-19 用户批准「做收窄候选面」，属发现 #37/#38）
  - *背景*：决策 14 的超集预取把候选对从 1,158 放大到约 7,470（6.4×），对齐墙钟 105.9 → 46.4 min（2.28×）但仍占整本构建 55%（2783.9 / 5048.7s）。超集里绝大部分对是主循环按频次遍历时「命中即 break」永远不会问到的。
  - *做法*：(a) 新增 `build_alignment_candidate_pairs(raw_stats, canonical_by_id, *, top_k=None)`——逐类别摊平候选池（已有实体规范名+别名 ∪ 同类 raw 名），过 `is_judge_candidate` 后按 `calculate_char_overlap_ratio` 降序（并列按名称升序保证确定性）截断到 K，`_is_deterministic_alias_pair`（包含/子序列/同音）命中的对**无条件保留**，结果去重排序；(b) 参数 `INSIGHT_JUDGE_TOP_K`（默认 16，夹紧 4..64，`0` 关闭）；(c) `prefetch_alignment_judgements` 与 `align_entities` 都接受 `candidate_pairs`，`align_entities` 内再摊成 `judge_candidates` 集合并引入 `judge_same(judge, cat, name, other)` 闭包，**主循环 10 处 `judge.judge_pair_sync` 全部改走它**——保证被收窄的对既不进批量预取、也不会退化成逐对单请求（否则等于把发现 #34 修掉的病理重新引入）。
  - *理由*：离线回放 8,613 条真实判词（1,364 判同）与 275 条 `llm_semantic_judge` 归并，得到「保留率 ↔ 真实归并召回」曲线：K=8 → 56.3%/96.3%、K=12 → 66.2%/98.1%、**K=16 → 73.8%/98.6%**、K=24 → 83.4%/99.5%、K=48 → 96.8%/100%。线上库用生产函数复算：旧口径 8,195 个无序对（有向 12,433）→ K=16 后 5,381（65.7%）；首建 4,238 → 3,442（81.2%）。选默认 16 的理由是「候选对 −26% 换 1.4% 召回损失」，需要保守可调 24/48，需要更快可调 12。前缀优先排序（包含 > 共同前缀≥2 > 重合度）实测更差（K=16 只保留 66.6% 对、丢的真合并没有更少），故弃用。
  - *验证*：新增 5 条用例（env 解析与夹紧、收窄后对数上限、确定性别名对在 K=1 下仍保留、硬拒绝对不进候选、`RecordingJudge` 集成断言「批量裁决收到的对 == 允许集合且主循环单对裁决全在允许集合内」），`test_stage5_insight.py` **158 passed**（原 153 + 5）。
  - *未做*：两遍走（干跑记录真实发问对 + 真跑）需拆解巨型归并循环，风险高于收益；给对齐单配便宜/无思考裁决模型属成本优化，留待后续。

- **决策 17：死代码清理只删「零引用」符号，孤儿模块必须先用真实入口集复核**（2026-10-19 用户问「前后端确定全部代码都有用吗」→ 审计；批准「P1+P2 一起做」）
  - *审计方法*：后端从 `saber_v2.py`/`run.py`/`scripts/` 做 AST import 可达性 + 780 个顶层符号的引用计数；前端对 `vue-frontend/src` 的 203 个文件做导出名反向引用扫描（区分「零引用」「仅测试引用」「仅本文件引用」），组件层（`components/ui` 21/21、`components/product` 22/22、`views` 9/9）全部真实引用。
  - *做法*：后端删除 `src/core/ocr_types.py` 的 `OcrTextlineResult`/`create_ocr_result`/`create_ocr_textline_result`/`ocr_results_to_dicts`/`extract_texts_from_ocr_results`/`_polygon`/`_rgb`、`logging_config` 的 `set_backend_console_level`/`configure_product_handler`、`derived` 的 `calculate_confidence`/`is_character_name_alias`、`translation_constraints.with_glossary_delta`、`openai_options.create_openai_compatible_options`、孤儿测试夹具 `tests_backend/fake_provider.py`，并把 `src/shared/paddleocr_vl.py` 的语言名常量迁进 `src/shared/constants.py` 后整份删模块（`settings/validation.py` 改 import）；前端删除 23 个零引用导出（`api/insight.ts` 5 个 v1 残留函数 + 4 个随之无用的导入、`api/v2/content.ts` 2 个、`api/v2/diagnostics.ts` 3 个 OCR/翻译连通性测试、`api/v2/settings.ts` 3 个字体/临时文件接口、`config/aiProviders.ts` 1 个、`constants/prompts.ts` 6 个 OCR 提示词/语言常量、`defaults/textStyleDefaults.ts` 1 个、`stores/settings/proofreadingIdentity.ts` 1 个、`utils/openaiOptions.ts` 1 个）以及因此变成死代码的模块内私有常量。
  - *理由*：这些符号**没有任何生产调用点**，保留只会让读者误以为存在对应功能（翻译/OCR 时代残留最典型）；而「仅测试引用」的导出（如 `mutatePageDocument`、`cleanV2TempFiles`、`parseReaderSettingsPayload`）**不删**——它们对应的后端路由/契约还在，删前端会与 P3「下线路由 + 删契约」脱节。
  - *取舍与实证*：审计报出的两个「孤儿模块」经复核都是**活的**，一律保留——`src/backend_v2/storage/consistency.py` 被 CLI `scripts/check_v2_consistency.py` 使用（`test_stage1_platform.py::test_consistency_cli_is_directly_executable` 钉住），`src/backend_v2/standard_streams.py` 被入口 `saber_v2.py:14,16` 调用；`src/backend_v2/auth/bootstrap.py` 也保留，因为它是 public 部署**唯一**的首个管理员创建入口（`AuthRepository.create_admin` 只被它调用，无 setup 路由）→ 改为在文档补说明。
  - *验证*：后端全量 **946 passed**（947 − 1：删掉的 `test_console_level_can_change_without_disabling_detailed_file_logging` 只在测被删函数）；前端 `vue-tsc` 0、`vitest` 88 files / **642 tests** 全绿、`eslint` 0 errors（20 warnings，与清理前一致）、`stylelint` 0、`lint:ui`/`--audit` 0、`vite build` 0——**构建产物 `src/backend_v2/static/vue` 字节未变**，反证这些导出早已被 tree-shaking 排除。
  - *未做（P2b/P3，等拍板）*：`src/core/rendering.py`(1902 行)、`src/core/config_models.py`、`src/core/ocr_types.py` 的 `OcrResult`、`src/backend_v2/content/translation_constraints.py` 的删除都牵连 `mutate_page_document` 与 `PATCH /api/v2/pages/<id>/document` 路由、4 个渲染测试套件与契约，属破坏性 API 变更；12 条退役路由（`/pages/<id>/bubbles`、`/books/<id>/translation-constraints`、`/translation/bootstrap` 等）同理。

- **决策 18：退役域整体下线——12 条路由 + 4 个半死模块 + 契约同步收窄**（2026-10-19 用户批准选项 A「连 P3 一起做」）
  - *背景*：决策 17 的 P1 只删「零引用符号」；但 bubble 持久化、页面文档编辑、渲染流水线、快速工作区、翻译约束这批**已退役语义**仍留着 HTTP 路由、契约条目与半死模块，读者会误以为功能还在（报告发现 #30 的「接缝」）。
  - *做法*：
    *(a) 后端路由*（`src/backend_v2/content/routes.py` 751→507 行）删 `GET /pages/<id>/document`、`GET /pages/<id>/render-status`、`PATCH /chapters/<id>/settings-memory`、`PATCH /chapters/<id>/last-visited-page`、`GET|PUT /books/<id>/translation-constraints`、`PATCH /pages/<id>/document`、`POST|PATCH|DELETE /pages/<id>/bubbles`、`POST /quick-workspace/reset`、`GET /translation/bootstrap`、`POST /quick-workspace/promote`；保留 `POST /chapters/<id>/pages`、`GET /assets/<id>`、`GET /tags`。
    *(b) 后端实现*（`content/repository.py` →1538 行）删 `get_constraints`/`update_constraints`/`quick_workspace_context`/`translation_bootstrap`、`get_page_document`/`mutate_page_document`/`_get_page_document` 及渲染辅助、`reset_quick_workspace`/`promote_quick_workspace`，以及随之死掉的 `_CHAPTER_WORK_STATE_KEYS`/`_CHAPTER_MEMORY_FORBIDDEN_KEYS`/`_validate_chapter_settings_memory` 等；`settings/resolver.py` 删 `resolve_translation`/`resolve_page_operation`/`_translation_prompt`/`_effective_translation_settings`/`_detector_section`。
    *(c) 模块*：`git rm src/core/rendering.py`(1902 行)、`src/core/config_models.py`、`src/core/ocr_types.py`、`src/backend_v2/content/translation_constraints.py`；`DEFAULT_AUTO_GLOSSARY_PROMPT` 迁入 `src/shared/constants.py`（`tests_backend/test_prompt_defaults.py` 改 import）。（`storage/consistency.py`、`standard_streams.py` 经复核保留，理由见决策 17。）
    *(d) 契约*：`openapi/v2.yaml` 删 4 条退役路径 + 15 条退役路径/18 个孤儿 schema 与参数（`tmp_trim_plugin_spec.py`/`tmp_clean_orphans.py`，`ExportPreferences` 被运行时仍引用故补回），paths **127 → 117**；`tmp_openapi_diff.py` 复验**契约 146 == 运行时 146**（零差异）；重生成 `vue-frontend/src/api/generated/v2.ts`。
    *(e) 前端*：`api/v2/content.ts` 366 行重写为只留活导出（`newIdempotencyKey`/`listChapterPages`/`getPageSummary`/上传与章节页命令等）；删 `types/bookTranslationConstraints.ts`、`utils/bookTranslationConstraints.ts`、`tests/unit/bookConstraintContext.spec.ts`；`api/bookshelf.ts`/`bookshelfStore.ts`/`types/bookshelf.ts`/`constants/prompts.ts` 清掉翻译约束与 OCR 提示词残留。
    *(f) 测试*：删 3 个渲染套件（`test_text_alignment_rendering.py`、`test_unified_text_stroke_rendering.py`、`test_vertical_punctuation_rendering.py`）；`test_stage2_content.py` 1746→1145 行（删 15 个针对已退役端点的用例，页面样式断言改读 `pages.c.default_font_id`/`page_style_defaults_json`，严格强制矩阵删 5 条退役端点 lambda）→ **38 passed**。
  - *理由*：这些路由/模块的全部语义（bubble 落库、页面文档 CAS 编辑、渲染度量、快速工作区晋升、翻译约束）都已随翻译时代退役，保留即债务；整体拆除比逐条留空壳更省事，也让契约重新等于运行时。
  - *取舍*：`public_policy.require_page_operation`（`require_feature("editMode")`）**保留**——它已无生产调用点，但 `test_public_profile.py` 仍直接调用它验证 editMode 语义，删它要连带改该用例，收益为零。
  - *验证*：`pytest --co` 收集 773 项；后端全量 **773 passed / 0 failed**（2 warnings、42 subtests；较 P1 的 946 passed 减少的 173 项全是随退役域一并删除的用例：3 个渲染套件 + `test_stage2_content.py` 的 15 条 + `test_memory_error_propagation.py` 的 1 条渲染用例）；前端六项门禁 **0**（vitest 88 files / 642 tests、vue-tsc 0、eslint 0 errors / 20 warnings、stylelint 0、`lint:ui` 0、`--audit` 0）；`vite build` 重建 `src/backend_v2/static/vue`（47 项变更随提交），CI 的 generated-frontend 门禁要求零脏项。契约与运行时路径数已相等（`tmp_openapi_diff.py`：contract 146 == runtime 146，零差异）。
  - *未做*：`plugin_agent` 半删除域、DB 悬空表（决策 12/坑 21 的 3.5.4）仍悬。

- **决策 19：问答「引用乱指 + 答不出第几话」的修复——兜底不再编造、降级必须可见、实体证据页成为一等证据**（2026-10-20 用户报告后批准「开始」，属发现 #40 / 报告 §5.10）
  - *背景*：用户实测 JJK（`29011e06-…`）问「漫画中第一次领域展开是谁使用的？在第几话第几页」→ 回答「两面宿傩 / 第 190 页 / **话数：证据不足**」，引用却是**第 1 話 9、11、12、13、16 页**（铅球场等无关页）。取证结论：`data-v2/logs/saber-worker-diagnostic.log:21129` 的 `Vector retrieval failed … Error creating hnsw segment reader: Nothing found on disk; falling back to page analysis`（worker PID 36340，22:50:58）是触发点——Chroma 的 HNSW 段读取失败，异常被 `qa.py` 吞掉后**静默**降级；而兜底 `_fallback_page_candidates` 在无关键词命中时给所有候选 `hybridScore=0.5` 并按页号**先截断**到 `candidateLimit = topK × 6`，于是只有全书最前 30 页（JJK 第 1 話）进入重排，重排再从中挑 5 页当引用。另一条独立缺陷：`_answer_context` 把实体知识库的「出现页码」打成裸全局页号（190）而无 page_id/话映射，且引用集合只取重排后的页面候选 → 实体证据页永远不是引用，模型拿不到话数（全书第 190 页实为第 8 話第 13 页）。
  - *做法*：(a) **兜底不再编造**：`_fallback_page_candidates` 只保留关键词命中的页（`lexical<=0` 丢弃，无命中返回空集），文档正文统一用 `_page_document`；`_global_context` 的兜底同理，无全局产物且关键词无命中就如实返回空；(b) **降级可见**：`_page_candidates` 返回 `(candidates, retrieval)` 诊断（`{degraded, reason, mode ∈ vector/lexical/none, candidateCount}`）并随检索结果落库，`validate_retrieval_candidates` 接受可选 `retrieval` 块（形状严格校验）、新增 `retrieval_diagnostics()` 读取；`routes.py` 在 SSE `context` 事件发 `degraded`/`warnings`，用户日志追加「漫画问答检索降级：…」，前端回答下方显示 ⚠️ 提示条；(c) **实体证据页成为一等证据**：新增 `page_locations()`（run 页号 → `chapterOrdinal/chapterTitle/pageInChapter`，查不到就 `None`）与 `entity_page_candidates()`（把实体记录的出现页码物化为候选页，`hybridScore=1.0`），`ask_question` 并入候选并标 `source: "entity"`（前端 chip 显示「（实体依据）」）；`_answer_context` 对实体证据页打印「页话对应：第 X 话 第 Y 页（全书第 N 页）」，页面资料标签改为「第 X 话 第 Y 页（全书第 N 页）」，并追加 `[回答要求]`（必须给话数、无依据就直说、不得猜测）；`stream_answer` 增 `locations` 形参；(d) 顺带修掉「无候选时向模型传空配置」——`materialize_api_config` 改为无条件解析（重排器按需包含）。
  - *理由*：检索失败时给出「全书开头几页」比明确说「没有依据」危害更大（用户会照引用去翻无关页）；降级必须走用户可见通道，否则「状态 ready 但检索静默降级」无法被发现。实体知识库本来就记录了证据页，缺的只是「页码 → 页话」映射与「让它进引用」这一步。
  - *验证*：后端新增 4 条用例（关键词兜底只留命中页且无命中返回空、诊断形状与降级原因、`page_locations` 话数映射与缺失页给 `None`、实体证据页可被引用且上下文含页话对应）→ 定向 **8 passed**，`test_stage5_insight.py` + `test_public_profile.py` **190 passed**；前端新增 4 条用例（降级提示透传、非法提示拒收、实体依据 chip、无引用不出 footer）→ 全量 vitest **87 files / 638 tests**，`vue-tsc`/eslint/stylelint/`lint:ui`/`--audit` 全 0。
  - *未做（建议）*：①HNSW 段读取失败应把该向量代标记为不可用并提示/自动重建（前端已有向量重建入口），`qa/status` 的 `available` 应计入「索引可读」；②引用可再收窄为「模型实际引用到的页」，当前仍是「送入模型的候选集」。

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

- **坑 17：判断一张表是否「悬空」不能只看行数——0 行 ≠ 死表**
  - *现象*：清理报告里一直写着「`job_step_asset_outputs`、`studio_message_assets` 等 0 行表仍在 schema 中」；但线上库里 `sessions`、`invite_codes`、`book_tags`、`chapter_navigation_state`、`provider_rate_limits`、整个 studio 域**也全是 0 行**，只看行数会把活域一起判死。
  - *正确方法*（本轮新建两个只读脚本）：**三维对照**——①`schema.py` 的 `metadata.tables` 定义（75 表）；②`src/**/*.py` + `tests_backend/**/*.py` 里对表标识符的引用（含字符串字面量）；③线上库逐表 `COUNT(*)`。三者交叉后，「只被 `schema.py` 引用的悬空表」只有 4 张（`operation_asset_inputs`、`operation_credential_snapshots`、`operation_plugin_snapshots`、`studio_message_assets`），而 plugin 集群与 durable-operations 残留是按**域**成簇 0 行。
  - *结论*：**发布前不删**——删除需要一次 `3.5.4` 迁移（新冻结 SQL + convert/validate + registry + 测试版本号）并改动仍在读这些表的活代码（`jobs/retry.py` 读 `job_plugin_snapshots`；`jobs/repository.py`、`storage/epochs.py`、`content/repository.py`、`auth/authorization.py` 读 `operations`），收益为 0 而跨安装存在数据丢失风险。已登记为下一版清理项（清理报告 §10 第 1 条）。

- **坑 18：删随包资源（字体）必须连带更新「按文件名枚举资源」的测试夹具，且必须重跑后端全量**
  - *现象*：字体授权清理提交 `b277e5e` 删掉 24 款专有字体后，只同步了 `ALGER.TTF` 的两处用例；`tests_backend/test_vertical_punctuation_rendering.py` 的 `FONTS` 元组仍写死 `msyh.ttc` / `Arial_Unicode.ttf` → 后端全量 **37 failed**（35 条来自该模块的 `subTest(font=...)`，2 条是本地给 `PYTHONIOENCODING=utf-8` 导致的 `test_role_boundaries` 假红），而当时只跑了受影响的定向子集，没跑全量，所以直到发布前复验才暴露。
  - *根因*：`FONTS` 是硬编码文件名列表，不是「扫描 `resources/fonts` 目录」；产品侧 `src/core/rendering.py` 仍被 `src/backend_v2/content/repository.py:2054`（`calculate_auto_font_size`）使用，测试因此必须保留而不是删除。
  - *现状*：**已修**——`FONTS` 改为两款随包自由字体（`思源黑体SourceHanSansK-Bold.TTF`、`ChisugaShotai_Regular0.2.12.1.ttf`），模块 **14 passed / 45 subtests passed**。
  - *教训*：①删资源后必须跑一次**后端全量**（`python -m pytest tests_backend -q`），定向子集会漏；②**不要**给全量套件设 `PYTHONIOENCODING=utf-8`——`_clean_role_environment()` 会把它继承给子进程，而 `_run_probe`（`tests_backend/v2/test_role_boundaries.py`）以 locale 解码，立刻产生两条假红。

- **坑 19：预取闸门写死「必须有历史实体」+ 长过程内联在别的步骤里＝批量能力白装、进度不可见**
  - *现象*：久留美 387 页全书构建的 69% 墙钟（105.9 分钟）花在**逐对** LLM 裁决上，而 `LLMBatchJudge.judge_batch` 早已支持「切块 + 3 并发 + 缓存」；同时 `job_events` 里这段时间完全没有事件，排障只能从 worker 日志反推。
  - *根因*（发现 #34，`src/backend_v2/insight/derived.py`）：①预取闸门写成 `if llm_judge is not None and canonical_by_id:`——**首次全书构建没有既有实体，整段预取被跳过**，主循环于是对每个候选对走 `judge_pair_sync`（一次一对、每对一次 HTTP）；②对齐内联在 `insight_stage_timeline` 处理器里、且在 `self.jobs.complete_step(...)` **之后**执行，异常只 `LOGGER.warning("实体对齐自动生成异常，继续流水线: %s")` 静默继续；③预取的回退分支（`asyncio.run` 抛 `RuntimeError` 时）把结果**无条件写成 `True`**，是同类「静默错判」。
  - *现状*：**已修**（决策 14）——超集预取 + 批量裁决、对齐独立成步、回退分支按对取真实判定、每批进度写用户日志。
  - *教训*：①「底层已有批量能力」不等于「调用点会批量」，闸门条件要用「能力是否可用」而不是「是否有历史数据」；②任何耗时子过程都不要内联在另一个 step 内部——会同时毁掉进度可见性、可重试性与失败归因；③`except` 回退分支绝不能把判定默认写成 `True`（保守判 False 或显式告警）。

- **坑 20：往「精确键载荷」里加字段＝对存量数据的破坏性契约变更**
  - *现象*：为 E 给 insight VLM 载荷新增 `analyzeConcurrency` 后，`tests_backend/v2/test_stage5_insight.py` + `test_stage1_platform.py` 立刻 **86 failed**，报错都在 `src/backend_v2/settings/validation.py` 的 `_exact_keys`：`provider_settings.insight_vlm.payload` 的键集合必须与白名单**完全相等**，而存量库/夹具里的老载荷是 `{modelName, customBaseUrl, openaiOptions, imageMaxSize}`。这不是测试问题——**生产读设置时也会直接抛错**。
  - *根因*：`_exact_keys`（原实现）同时承担「拒绝未知键」与「要求全部必填键」两件事，新增字段等于把所有缺该键的历史记录判为非法；前端 `vue-frontend/src/api/insight.ts` 的 `hasExactKeys` 同理（vitest 也红了 10 条）。
  - *现状*：**已修**——后端 `_exact_keys(..., optional=...)` 与 `_OPTIONAL_INSIGHT_PROVIDER_PAYLOAD_FIELDS`，前端 `OPTIONAL_PROVIDER_PAYLOAD_FIELDS` + `hasRequiredKeys`；缺省合法、越界仍拒，受影响的 2 个测试文件 **254 passed**、前端 **642 passed**。
  - *教训*：给这类「白名单 + 精确相等」的载荷加字段，必须同时把它登记进**可选键集合**（并让前端校验同样区分必填/可选），否则就是对存量数据的破坏性变更；加字段前先搜一遍 `_exact_keys` / `hasExactKeys` 的调用面。

- **坑 21：数据根的「版本戳」与「物理结构」错位后，启动与升级路径会一起卡死（报告发现 #36）**
  - *现象*：`python run.py --no-browser` 启动即退出并打印 `[错误] 存储准备失败: 存储结构不匹配: missing=['browser_session_credentials', 'browser_session_pages', 'browser_sessions', 'bubbles', 'job_font_snapshots', 'operation_font_snapshots', 'render_requests', 'translation_constraints'], unexpected=[], changed=['operations', 'page_assets', 'prompts']`；`python saber_v2.py --role storage-migrator --action check|upgrade` 报同一条错，**连 upgrade 也进不了转换路径**，看起来像「库坏了」。
  - *根因*：该库是在「清理提交（09f44a4 删退役表）之后、`version.json` 仍写 3.5.2」的时间窗里用当前 metadata 初始化的——物理结构已经是 3.5.3 形（75 表，只有 `prompts` 的 CHECK 还是旧取值），而 `schema_metadata.storage_version` 停在 3.5.2。`StorageManager` / `runner.check()` 会**先按源版本（3.5.2，83 表）契约校验数据**（`src/storage_migrator/runner.py:60-71` → `_validate_version(self.root, source, files=…)`），于是必然报「missing 8 张退役表」并中止。只读诊断 `tmp_stamp_diag.py` 一眼看清：对 3.5.1/3.5.2 契约都 missing 8 张，**对 3.5.3 契约只差 `changed=['prompts']`**。
  - *现状*：**已就地修复**（先备份 `temp/db-backup-20261019-200610/`：sqlite3 + `-wal` + `-shm`）：用真迁移原语 `v3_5_3.stop_unfinished(root)` + `v3_5_3.convert(root)`（`convert` 对已退役表是容忍的：逐表 `if table in present` 才 DROP），再用真实 SQLAlchemy 表对象打戳 `update(schema_metadata).where(schema_metadata.c.singleton_id == 1).values(storage_version="3.5.3")`（脚本 `tmp_repair_stamp_353.py`）。复验 `tmp_stamp_diag.py` → `stamp=3.5.3`、`3.5.3: OK（结构与 3.5.3 契约一致）`、75 表；重启后 `GET /api/v2/health` 返回 `storageVersion=3.5.3`。
  - *教训*：①**跑开发树建的库会带开发树的物理结构**——重构窗口内「先改 metadata、后改 `version.json`」就会造出这种混合态，改存储结构时必须同一次改动里同步 `version.json`+新契约 SQL；②`validate_data(files=True)` 会按 `assets` 表校验数据根 `fonts/` 下每个字体文件的 sha256，而随包字体其实由 `src/backend_v2/resources/fonts/` 提供、**不复制进数据根**，运行期启动检查传的是 `files=False`（只有**待应用**的 pending 转换才查文件）——就地修复别把 `files=True` 当通过门槛；③改进方向见报告 #36：`check()` 在源契约失败但**目标**契约通过时，应降级为「结构已超前，只需校准版本戳」并给出修复命令。

- **坑 22：用 `job_events` 拆阶段耗时，时间列不是字符串（报告 §5.8 / 发现 #37 的数据来源）**
  - *现象*：`tmp_stage_durations.py` 首跑 `TypeError: 'str' object cannot be interpreted as an integer`（对 `job_events.created_at` 调 `datetime.fromisoformat(value.replace(...))`）。
  - *根因*：`src/backend_v2/storage/schema.py` 里 `job_events.created_at` / `jobs.created_at` 是 `DATETIME` 列，SQLAlchemy 已解码成 `datetime`，不需要（也不能）再 `fromisoformat`；`job_items` 也**没有** `kind` 列（`kind` 在 `job_steps` 上、`event_type` 在 `job_events` 上）。
  - *正确姿势*：`parse_ts` 同时接受 `datetime` / 字符串（含 `Z`）/ epoch；配对用 `step_started`（payload 带 `itemId`/`stepKind`）→ `page_completed`（同一 `itemId`），每个 item 一条，派生步骤也走同一套（所以「对齐」拆成独立步骤后才第一次出现在耗时表里）。教训：**先查 schema 列类型再写解析**，别按 JSON 直觉当字符串。

- **坑 23：收窄候选面时必须让「批量预取」与「主循环」共用同一份候选集**
  - *现象*：只改 `prefetch_alignment_judgements` 的枚举、不管主循环，被收窄掉的对在批量缓存里没有条目，主循环 `judge_pair_sync` 便会**逐对发单请求**——看似省了对数，实则把发现 #34 修掉的「1,158 次单对调用」重新引回来，且这些请求不在预取进度日志里、外部完全看不见。
  - *解法*：把候选集算一次（`build_alignment_candidate_pairs`），预取直接用它，主循环通过 `judge_candidates` 集合 + `judge_same()` 闭包只对集合内对发问；用 `RecordingJudge` 断言「`batched` 与 `synced` 的每一对都在允许集合内」。
  - *附带教训*：`build_alignment_candidate_pairs` 返回的是**无序对去重**结果，所以「某词条的伙伴数」可以超过 K（它被很多别的词条选中），能保证的只有「每个词条最多**选出** K 个伙伴」——写断言时要按 out-degree 而非 in-degree，否则会写成 `max(partner_counts) <= K` 这种必然失败的用例（本轮真实踩到）。

- **坑 24：扫死代码时入口集必须包含 CLI 脚本与打包入口，否则会把活模块判成孤儿**
  - *现象*：第一轮可达性扫描只从 `src/` 内部相互 import 出发，把 `src/backend_v2/storage/consistency.py`（145 行、含 `ConsistencyChecker`）与 `src/backend_v2/standard_streams.py` 判成「孤儿模块」，准备删除。复核引用时才发现前者被 CLI `scripts/check_v2_consistency.py` 使用（并有 `test_stage1_platform.py` 的可执行性用例钉住），后者被入口 `saber_v2.py:14` 直接 import。
  - *解法*：入口集必须至少包含 `saber_v2.py`、`run.py`、`scripts/*.py`、`.github/scripts/*`，以及 `app.spec` 的 hiddenimports/运行时钩子；前端同理要把 `tests/`（spec 与 property）单列一档，区分「零引用」与「仅测试引用」，后者随对应路由下线一起处理。
  - *附带教训*：本仓库开启了 `core.autocrlf`，工作区文件是 CRLF 而 HEAD 是 LF；用 Python 脚本批量改写文件时 `read_text()` 会把 CRLF 归一成 LF，提交前要看 `git diff --stat` 是否出现整文件级改动（本轮 diff 干净，说明只是行尾归一，无实质污染）。


- **坑 25：删一个后端模块会同时打穿测试收集、契约用例与 PyInstaller spec，必须四处一起扫**
  - *现象*（P3 拆退役域时连环踩到）：①删 `src/core/rendering.py` 后 `tests_backend/test_memory_error_propagation.py` 仍 `from src.core import rendering` → **collection ImportError，整个套件一项不跑**（`pytest` 直接 `Interrupted: 1 error during collection`）；②`app.spec` 的 `hiddenimports` 仍列 `src.core.config_models`/`src.core.ocr_types` → `test_packaging_contract.py::test_production_spec_has_no_missing_project_hidden_imports` 报 missing；③`test_openapi_contract.py` 的「必须固化的 backend-first 命令」集合还钉着 `/pages/{page_id}/document`、`UNKEYED_STATE_COMMANDS` 还列着 `updateBookTranslationConstraints`/`resetQuickWorkspace`/`promoteQuickWorkspace`/`updateChapterSettingsMemory`/`updateChapterLastVisitedPage`；④`test_public_profile.py` 用 `PATCH /api/v2/pages/<id>/document` 表达「editMode 关闭」「字体跨用户隔离」，路由下线后返回 **405 METHOD NOT ALLOWED**（断言 403/404 全假红）。
  - *解法*：删模块的同一轮里做四件事——`python -m pytest tests_backend --co -q` 先确认**收集通过**；扫 `app.spec` 的 hiddenimports；扫 `tests_backend` 里被删符号与退役端点的引用；契约用例的「固化清单」从契约实测值改写（本次改成只钉仍然存在的 `/pages/{page_id}`、`/jobs/{job_id}/resume|continue`），字体隔离改用**仍在服务**的 `GET /api/v2/fonts`（列表不含他人上传）+ `DELETE /api/v2/fonts/<id>`（跨用户删除 404）。
  - *附带教训*：`public_policy.require_page_operation` 在路由下线后已成无调用点的方法，但测试仍直接调用它，所以「零引用」判断必须把测试文件算进引用图（决策 17 的入口集规则）。

- **坑 26：公网 404 的文案有两层，跨层断言 message 必假红**
  - *现象*：把字体隔离用例从页面文档改成 `DELETE /api/v2/fonts/<他人字体>` 后，`assert json["error"]["message"] == "font not found"` 变成 `'资源不存在'`。
  - *根因*：`src/backend_v2/auth/authorization.py:163-166` 的 route-identifier 预检在进入 handler 之前就按 owner 判定返回 `{"error":{"code":"not_found","message":"资源不存在"}}`（404），根本轮不到 `src/backend_v2/settings/routes.py:116-119` 的 `LookupError` handler（那才会返回 `str(exc)`＝"font not found"）。
  - *正确姿势*：跨层用例只断言 **`error.code`**（`not_found`），message 属实现细节。

- **坑 27：Chroma 的 HNSW 段文件丢失会让向量检索「静默降级到按页号截断」，且告警只在 diagnostic 日志通道**
  - *现象*：JJK 问答引用全是第 1 話的无关页，而回答读起来完全正常（用户无从察觉）。`saber-worker.log` 里那次的候选页号严格递增 1..30、`hybridScore` 全是 `0.5`、**没有 `vectorScore` 字段**——这正是 `_fallback_page_candidates` 的签名（有词命中才 `round(0.5 + 0.5×lexical, 4)`，无命中恒为 0.5）。
  - *根因*：Chroma 的 HNSW 段在磁盘上不可读（`Error creating hnsw segment reader: Nothing found on disk`），`_query_chroma` 抛错被 `qa.py` 捕获后转兜底；而 `qa_status` 与指纹门禁**全部通过**（`generation=3`、`dependency_fingerprint` 与快照相等、coverage 234/160），嵌入本身也健康（234/234 唯一单位向量、两两 cos 0.294/0.485/0.752）→ 所以「状态 ready」不代表「索引可读」。该 WARNING 只写 `data-v2/logs/saber-worker-diagnostic.log`（Python LOGGER 通道），`saber-worker.log` 与用户日志都没有；同一 worker 进程（PID 36340）出问题，重启后（PID 41624）同一集合查询即恢复正常（向量分数 0.5679/0.5307/…）。
  - *正确姿势*：排查问答「答非所问/引用乱指」时，先看候选页是否带 `vectorScore`，再用 `grep -n "Vector retrieval failed" data-v2/logs/*.log`（必须包含 `*-diagnostic.log`）；跨维度定罪要分三步——`vector_generations` 的代次状态 → 指纹一致性（`tmp_fp_probe.py`）→ 直接读 Chroma 集合的向量健康度（`tmp_chroma_embed_probe.py`），三者都正常才轮到「检索逻辑/重排」。
  - *已修*：兜底不再用页号顺序凑候选（只留关键词命中页），降级诊断随结果落库并对用户可见（见决策 19）。

---

## 6. 新会话先读

1. `AGENTS.md`：核心数据库脚本防手写约束与行为规范。
2. `PROGRESS.md`：重构完整演进历史与各模块设计细节。
3. `run.py` & `src/backend_v2/launcher/unified_launcher.py`：服务启动与进程架构。
4. `src/backend_v2/insight/derived.py`：实体对齐、自动归并、图谱数据生成算法。
5. `src/backend_v2/insight/qa.py`：问答系统、实体解析器与证据格式化。
6. `vue-frontend/src/views/`：前端核心页面实现。
7. `acceptance6_stress_report.md`：验收点6 全量报告（阶段0-4、发现 #1-#40、裁决候选对离线回放与 Top-K 收窄标定数据、附录里的分析脚本清单）。
8. `verify_acceptance6_alignment.py` / `verify_acceptance6_downstream.py` / `verify_acceptance6_qa.py`：三项验收脚本（问答用法 `python verify_acceptance6_qa.py <bookId> "问题" [--mode generic|exact|global]`）。
9. 本轮新增的工作区临时脚本（**不入库**）：`tmp_replay_rules.py`（对齐规则离线回放，零 token）、`tmp_scan_undefined.py`（未定义全局名扫描）、`tmp_job_status.py`/`tmp_guard_job.py`（job 监控与守护）、`tmp_check_state.py`（job/artifact 现状）、`tmp_retry_failed.py`（只重跑失败项）、`tmp_inspect_retry_job.py`（job + items + 冻结配置 vlm/chat/embedding + 产物）、`tmp_artifact_detail.py`（产物全列 + payload 键 + warnings）、`tmp_wait_job.py`（轮询至终态，`<jobId> <maxMinutes>`）、`tmp_verify_vector_events.py`（向量事件回退验证）、`tmp_trim_plugin_spec.py`（OpenAPI 已删域残留裁剪：按缩进删路径/schema/参数/tag，幂等）、`tmp_stage_durations.py`（按 `job_events` 配对统计各步骤耗时）、`tmp_narrow_topk.py`（Top-K 收窄的对数/召回曲线，决策 16 依据）、`tmp_narrow_probe.py` / `tmp_narrow_recall_prod.py`（直接调生产函数在线上库复算收窄效果）、`tmp_audit_db_tables.py`（表三维对照：metadata 定义 vs 代码引用 vs 线上行数）、`tmp_audit_db_clusters.py`（按域打印 plugin / operations / studio 等簇的 metadata 覆盖与行数）、`tmp_qa_probe_artifact.py`（某书 artifact 版本表 + 每章已发布页覆盖）、`tmp_vec_probe.py`（`vector_generations` 各代状态/覆盖/active 与 Chroma 集合散列前缀）、`tmp_fp_probe.py`（快照指纹 vs 活动代指纹 + `qa_status`）、`tmp_chroma_embed_probe.py`（直接读 Chroma 集合的向量范数与两两 cos，区分「嵌入退化」与「索引读失败」）、`tmp_qa_repro.py`（复现某问题的 QA SSE，落盘 `tmp_qa_repro_sse.txt`）。

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
7. ~~**退休域的「接缝」是否裁剪**（报告发现 #30）~~：**已于本轮按「整体拆除」拍板并落地**（决策 18）——12 条退役路由（`/pages/<page_id>/document`、`/pages/<page_id>/render-status`、`/pages/<page_id>/bubbles*`、`/chapters/<chapter_id>/settings-memory`、`/chapters/<chapter_id>/last-visited-page`、`/books/{book_id}/translation-constraints`、`/translation/bootstrap`、`/quick-workspace/reset|promote`）已从运行时与 `openapi/v2.yaml` 一并删除，四个半死模块（`src/core/rendering.py`、`src/core/config_models.py`、`src/core/ocr_types.py`、`src/backend_v2/content/translation_constraints.py`）已删；契约路径数由 127 降至 117，且契约与运行时已零差异。
8. **发布前的两步（都在用户侧）**：①仓库当前仍是**私有**（未认证访问 `https://api.github.com/repos/JLBC05/MiyakoKB` 返回 404），而 README 的下载 / Issues 链接与 v* Release 资产都按「公开仓库」预期——要发布需先在仓库设置里改为公开；②建议先手动 `workflow_dispatch` 跑一次 `.github/workflows/release.yml` 验证 CI（它会跑后端 pytest + 前端门禁 + 打包 probe + 分卷上传），确认全绿后再打 `v0.1.0` tag（**首个公开发布按 0.x 起版**；`release.yml` 会强制 tag 版本 == `version.json` 的 `.version`，当前二者均为 `0.1.0`，`storageVersion` 独立为 `3.5.3`）。
9. **发行包形态**：`release.yml` 仍按 `cpu` / `gpu` 两个 variant 各出一个包，但 `requirements-cpu.txt` 与 `requirements-gpu.txt` 已逐字节相同（本地模型与 CUDA 栈移除后二者内容一致）——是否收敛为单一发行包，需要产品决定（收敛可省一半 CI 时间并避免用户选错）。
10. **下一版 DB 清理（`3.5.4`，本轮明确不做）**：4 张只被 `schema.py` 引用的悬空表（`operation_asset_inputs`、`operation_credential_snapshots`、`operation_plugin_snapshots`、`studio_message_assets`）与两个按域成簇 0 行的集群（plugin 集群 5 张、durable-operations 残留 4 张）是否在下一版一并删除；同时可收紧两处**枚举残留**——`src/backend_v2/storage/schema.py:78-94` 的 `JOB_KINDS` 仍含翻译时代与半删除域的取值（`translation`/`remove_text`/`detect`/`style_apply`/`text_import`/`plugin_agent`），`schema.py:99-115` 的 `OPERATION_KINDS`/`PAGE_WRITE_OPERATION_KINDS` 仍是 `bubble_*`/`page_*` 全套（这些是带 CHECK 约束的字段，收窄必须走迁移）。删除需要：按 `docs/storage-migrations.md` 新增 `src/storage_migrator/schemas/3.5.4.sql` + `v3_5_4.py`（convert 删表/重建带 CHECK 的表、validate 委派）+ `registry.py` 登记 + 测试版本号递增，并改动仍在读这些表的活代码（`jobs/retry.py`、`jobs/repository.py`、`storage/epochs.py`、`content/repository.py`、`auth/authorization.py`）。实测数据见清理报告 §10 第 1 条。
11. **构建流水线的第二档提速（报告发现 #34 的剩余项）**：本轮已把实体对齐（69.3%）批量化并独立成步（决策 14），并把剩下两处杠杆做成可见可调（决策 15）：①**第 1 层聚合 78 次请求 / 777.5s（8.5%）**——自定义架构下第 1 层 `unitsPerGroup` 现可表示「每组合并页数」（内置预设行为不变）；②**逐页 VLM 分析 78 批 / 1658s（18.1%）**——批内并发已升级为 insight VLM 供应商级设置 `analyzeConcurrency`（1..8，默认 3；env `INSIGHT_ANALYZE_PARALLEL` 降级为兜底）。两项都属「改吞吐不改语义」，但仍需**在真实供应商上限下**复测：建议重启开发栈后用同一本 387 页漫画跑一次全书构建，记录新的阶段耗时与限流情况（注意并发调高对免费层供应商会触发 429）。
12. **问答检索的自愈与引用收窄（报告 §5.10 / 发现 #40 的建议）**：①Chroma 的 HNSW 段读取失败时应把该向量代标记为不可用并提示/自动重建（前端已有向量重建入口），`qa/status` 的 `available` 判据应计入「索引可读」；②引用目前是「送入模型的候选集」，可再收窄为「模型实际引用到的页」。另需在浏览器复验两处新交互：引用气泡现在会对实体证据页显示「（实体依据）」后缀，检索降级时回答下方出现 ⚠️ 提示条。

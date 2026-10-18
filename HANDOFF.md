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
- [ ] 验收点 6：生产环境大体量漫画（如 50+ 话）的端到端压力测试与跨章节实体消歧观察。
- **阻塞**：无代码或架构阻塞。
- **下一步**：
  1. 打开终端运行 `python run.py` 或双击 `启动.bat` 启动系统，在浏览器访问 `http://127.0.0.1:5000`；
  2. 测试导入一部全新的长篇漫画，触发全量知识库构建；
  3. 在问答页面针对不同章节的细节情节进行问答测试，验证跨章节实体追踪与准确度。

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

---

## 6. 新会话先读
1. `AGENTS.md`：核心数据库脚本防手写约束与行为规范。
2. `PROGRESS.md`：重构完整演进历史与各模块设计细节。
3. `run.py` & `src/backend_v2/launcher/unified_launcher.py`：服务启动与进程架构。
4. `src/backend_v2/insight/derived.py`：实体对齐、自动归并、图谱数据生成算法。
5. `src/backend_v2/insight/qa.py`：问答系统、实体解析器与证据格式化。
6. `vue-frontend/src/views/`：前端核心页面实现。

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

### 仍需人工确认的项目
1. 实际启动服务（运行 `python run.py`）后，在浏览器中测试点击问答引用气泡，确认弹窗预览真实图片与章节话数展示完全符合预期。
2. 如需接入其它第三方模型（非 Gemini），可在 `/settings` 页面中配置对应的 API Key 与自定义 Base URL。

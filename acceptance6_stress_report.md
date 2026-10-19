# MiyakoKB 验收点6 压测报告

> 状态：阶段0-4 的事实均已定稿（阶段3/4 见 §4/§5）；发现表滚动至 #25。
> 目标：生产环境大体量漫画（50+ 话）端到端压力测试与跨章节实体消歧观察。

## 1. 测试范围与环境

| 项 | 值 |
| --- | --- |
| 素材 | 呪術廻戦（Venera 导入，磁盘 148 话 / 2998 页 / 716.3MB） |
| bookId | 现用 `29011e06-5c43-41ab-905d-42891e2955de`（2026-10-18 14:23 重新导入）；早期 `4c33cc46-299b-4c5d-a9c1-5fc42ad1b961` 已不在库 |
| 运行栈 | `python run.py`（unified launcher）→ API(127.0.0.1:5000) + Worker，`--test-mode` |
| 验收样本（最终采用） | job `39c2c48f-3e56-4c43-a14f-e93e7d44a6e5` / run `bf374dbe-d391-4676-99ec-ac485f139399`，scope=chapter 前 10 话（234 页 + 10 衍生），2026-10-18 22:34–23:48 |
| 历史尝试 | job `804b8d94`（GLM-4.6V-Flash 免费层，21:09 取消，无产物发布）、job `b81c4a31`/run `87fa8174`（DeepSeek 调优，见 §3.4-3.6） |
| VLM 上游（最终采用） | 自建网关 `http://8.216.18.224:8317/v1`，`gemini-3.8-flash-high`，5 页/批、批内分片并发 1、未压缩图 |
| 图片压缩 | 历史调优配置 imageMaxSize=1024（最长边，Pillow LANCZOS → JPEG q85，实测 190–279 KiB/页）；本轮采用配置为默认 0（不压缩） |
| 基线参照 | FX久留美 387 页全量构建 46.4 分钟（≈7.2s/页）；DeepSeek 调优后吞吐 57 页/分钟，本轮旧网关约 10 页/分钟 |

导入核验：148 章 ordinal 1–148 连续无空洞；标题混用"第X話/简体X话"；第1話 53 页；含特别番外篇（ordinal 130）与番外篇（134）。

## 2. 阶段0：冒烟与环境事故（已定稿）

### 2.1 久留美构建 completed_with_errors 根因（考古定案）
387 页全部成功（failedCount=0），两个**环境性**阶段失败：
- `stage_overview_story_summary`：上游 502（"API 错误 502: "），流水线继续；
- `stage_vectors`：ChromaDB 未安装（requirements-cpu.txt:54 声明 `chromadb>=0.4.0` 为预期依赖，但环境缺失）。
- 发布态 status=completed_with_errors，failedItems=2。

### 2.2 pip 在运行栈上安装的三次事故与修复（可作为运维教训）
1. `pip install chromadb` 默认解析升级依赖树 → 运行中进程内存映射的 `.pyd` 锁死（WinError 5），yarl 被半卸载。修复：`pip install yarl==1.24.2 --no-deps`。
2. 第二次尝试卸掉 pydantic_core 2.46.4 / typing-inspection，新装再被锁 → pydantic 2.13.4 与 core 2.50.1 不兼容，新进程 `import pydantic` 即 SystemError。修复：`pip install --no-deps --force-reinstall pydantic_core==2.46.4`（未被锁的孤儿 .pyd 可安全覆盖）。
3. 最终策略：`pip install chromadb --no-deps` + 逐个探测补齐缺失依赖（pydantic-settings、opentelemetry 系列、grpcio），零锁风险。chromadb 1.5.9 冒烟通过（PersistentClient/get_or_create_collection/upsert/query/count/list/delete 全过）。运行中 Worker 懒导入 → 免重启生效。

### 2.3 旧产物考古（重要：对齐代码首跑）
- commit 8857592（10-17 16:25）才引入"timeline 后自动对齐"；久留美 10-17 08:48-09:35 的生产 run 跑的是旧代码，**当前对齐代码从未在生产规模执行过**，本次 JJK 构建为首次。
- 久留美库内 entity_alignment 产物（只覆盖 ch52-56）为开发会话手工插入，非生产流水线产物。
- 恋人书 10-17 23:54 run 每页遭上游内容安全拒绝（Google Generative AI Prohibited Use policy），重试 4-5 次后跳页磨完 → 环境级失败模式之二（内容审查），流水线韧性表现符合设计。

## 3. 阶段2：构建压测（历史阶段：DeepSeek 调优与事故；最终验收样本见 §4/§5）

### 3.1 时间线（2026-10-18，+08:00）
- 16:41 触发 `POST /api/v2/insight/analysis-jobs`（scope=full，3008 项 = 2998 页 + 10 衍生阶段）。
- 16:41–16:43 前 20 页正常（上游健康）。
- 16:43–17:12 **上游 502 风暴 #1**（GET /v1/models 直接探测 502 确证）。失败页以 ~30 项/分钟快速失败（每项 1 次请求+3 次退避重试 1.5s/2.7s/5.3s 后落 failed，不阻塞队列），累计 failed ~890。
- 17:12–17:33 上游恢复窗口（~21 分钟，完成 ~105 页，速率 ~5 页/分钟）。
- 17:14:11 第 951 批"开始流式返回"后**流式悬挂 ~2.5 分钟**（无 TCP 连接、proactor 空转、CPU 静止；py-spy 证实 MainThread 等待永不完成的 I/O，job-control 线程仅等取消信号，无进度看门狗）→ 17:16:20 自行恢复（961-990 页完成），第 991 页开跑。
- 17:16:44 人工击杀 Worker（打断的是活跃批次）→ launcher 2 秒拉起新 Worker（restart 1/5）。
- 17:17–17:26 **孤儿 job 死锁**（见 3.3）→ 人工收敛恢复 → 17:26 起断点续跑（completed=80 保留）。
- 17:27 /v1/models 探测 200（**误导**：models 端点健康不等于生成通道健康）。
- 17:33–17:53 **上游 502 风暴 #2**（再 +640 失败；18:00 实测最小 POST /v1/chat/completions 仍 502，上游持续故障中）。
- 17:53:43 **用户下令取消**（job=cancelled；终态 completed=185 / failed=1550 / cancelled=1273）。185 个已发布页保留。
- 18:00 用户决策：上游网关不可用，**切换 VLM 供应商为 DeepSeek 官方**（api.deepseek.com/v1，deepseek-flash）。
- 18:03–18:13 改造与装载：对齐语义裁决器改为跟随 Insight chat/vlm 设置（见 3.5 代码修复）；`PUT /api/v2/settings/transactions` 写入 insight_vlm/deepseek（credential + provider 行 + insight.vlm.provider=deepseek，insight rev 6→7）；重启 Worker 装载新代码。
- 18:13:44 重触发全量构建（scope=full，batchId 974577c6，jobId fb473678，runId b5663d85）。**首次实跑生产对齐代码**。
- 18:13:48 起按 5 页/批流式请求 DeepSeek；18:16 已完成前 20 页（4 批），速率 ≈8 页/分钟，请求形态与直连探测一致（见 3.4）。
- 18:17–18:26 稳定 10.5 页/分钟（130 页 / 0 失败）→ **用户中断**（18:26:49 cancel，completed=130）。
- 18:26–18:32 按用户要求调优三处（图片压缩 1024 / 关闭思考模式 / 批内分片并发，见 3.6），重启 Worker 装载新代码。
- 18:32:16 重触发（batchId 8af0ac35，jobId b81c4a31，runId 87fa8174）：首 2.5 分钟完成 125 页（≈57 页/分钟），failed=0（见 3.4）。


### 3.2 失败模式观察
- **1550 个失败 100% 同源**：`INSIGHT_BATCH_FAILED | "API 错误 502: "`（上游网关故障），零本地系统性失败；本地栈（API/Worker/DB）全程健康。
- 上游可用性模式：约 20-30 分钟一轮整体宕机，恢复窗口 ~20 分钟；/v1/models 200 不能作为生成通道健康判据。
- 请求级重试：1/4 次请求 + 3 次退避（1.5s/2.7s/5.3s，上限 60s），失败项秒级落 failed，不阻塞队列 → 故障期净效果是"快速磨完待办清单全部标失败"，重触发成本约为正常一遍的 1/4（无 LLM token 消耗，只有重试请求开销）。
- 流式中途连接死亡：无读超时兜底 → item 悬挂（本次 2.5 分钟自恢复，机制未定案）；job 系统无 item 级租约超时；launcher 看门狗只认进程退出。
- 单 Worker 串行 + `uq_jobs_one_current` 唯一槽：同一时刻仅一个执行中 job，其余排队。
- 故障期无长退避：上游宕机 30 分钟期间持续以 ~30 失败/分钟消耗队列（无熔断）。

### 3.3 【发现·高】test-mode Worker 崩溃 → 孤儿 running job（队列死锁）
- 现象：Worker 被杀后，新 Worker 就绪但不再推进；job 卡 status=running（attempt 79edf4dd 残留、worker_epoch_id=None、updated_at 冻结于死亡前最后进度写），`POST /jobs/<id>/continue` 409（running 不接受 continue）。
- 根因链：
  1. test-mode 下 Worker identity 为**固定字符串** `test-worker`（runtime_identity.py:113），新 Worker 启动时**删旧 epoch 行重插**且不收敛在途工作（worker/entrypoint.py:112-126）；
  2. unified_launcher 的崩溃重启路径**不调用任何 reconcile**（unified_launcher.py 全文无 reconcile；完整 launcher entrypoint.py:394 才有 reconcile_dead_worker → job→interrupted）；
  3. running 状态占住唯一 current 槽 → 新 Worker 只认领 queued → 死锁。
  - 生产完整 launcher 路径（UUID epoch + reconcile_dead_worker）不受此影响；属 test/dev 启动器缺口。
- 恢复 runbook（已验证）：调用生产收敛函数 `JobQueueRepository.reconcile_lost_worker_jobs(worker_epoch_id=None)`（running 项→pending、attempt 清空、落 job_interrupted 事件，completed 保留）→ `POST /api/v2/jobs/<id>/continue`（interrupted→queued）→ Worker 秒级认领续跑。

### 3.4 吞吐与资源（最终配置，18:32 起）
- 调优三件套（见 3.6）生效后：18:32:16 启动，18:34:28 已完成 125 页（2 分 12 秒 ≈ **57 页/分钟**），failed=0。单批实测 4.7–5.1 秒/5 页（日志：第 86 页批 5.08s、第 91 页批 4.72s）。
- 页面级 ETA：剩余 ≈2,873 页 ÷ 0.95 页/秒 ≈ **50 分钟**（约 19:25 完成页面）；衍生阶段（layer/overview×2/story_summary/timeline/vectors/align）预计再 10–20 分钟 → **整轮约 1.2–1.5 小时**。
- 成本估算（DeepSeek 官方定价，当前为 off-peak：输入缓存未命中 $0.15/M、输出 $0.6/M；峰值翻倍）：每批（5 页拆 2/2/1 三个并发请求）输入 ≈3×2,600≈7,800 tokens、输出≈1,800 tokens → ≈**$0.00225/批**；页面分析 600 批 ≈ **$1.35（≈¥10）**；衍生阶段 ≈$0.4–1.2（≈¥3–9）→ **整轮 ≈ ¥13–20**（off-peak；若落峰时段 ≈¥26–40）。
- 对照：思考模式开启时同批 43.6s 且 8,000 输出 tokens 全部被 reasoning 吃光（0 内容）——思考模式在长 JSON 抽取任务上既慢又贵。
- 资源：DB 30.0 MB；页面二进制 data-v2/objects 944.3 MB（构建期间源图+缩略图持续增长）。
- **误报澄清**：18:14 的"悬挂 8 分钟"判断为诊断滞后误报（py-spy 的 proactor `_poll` 即 asyncio 等待 I/O 的正常态；当时批次 1-5 已完成并继续推进）。旧网关 17:14 的悬挂为真实事件（无 TCP、无进度、>2 分钟静默），新上游未见同类。


### 3.5 VLM 供应商切换（DeepSeek）+ 对齐裁决器代码修复
- **触发**：原供应商网关（http://8.216.18.224:8317/v1）持续 502，无法支撑全量构建；用户提供 DeepSeek 官方凭据。
- **配置切换**（全程 API，无 UI）：`PUT /api/v2/settings/transactions` 单事务写入 ①credentialEdits（insight_vlm/deepseek，secret={api_key}）②providerSettings（insight_vlm/deepseek，modelName=deepseek-flash，customBaseUrl 留空走 manifest 默认，openaiOptions 沿用流式+JSON 模式）③settings.insight rev 6→7（vlm.provider=deepseek）。chat 段 `useSameAsVlm=true` → story_summary 等 chat 调用自动跟随；embedding（Cloudflare）/reranker（jina）不受影响。
- **【代码修复·高】对齐语义裁决器硬编码网关**：原 `get_default_llm_judge()` 固定指向 http://8.216.18.224:8317/v1 + gemini-3.8-flash-high（与设置脱钩，切换供应商后裁决仍打旧网关；`judge_batch` 异常被吞 → 全部判定 False → 语义合并静默失效）。修复于 `src/backend_v2/insight/derived.py`：`LLMBatchJudge.__init__` 增加 provider 参数；`align_entities` 增加 `llm_judge` 参数；10 处调用点改为优先使用注入裁决器；生产调用点改为按 frozen chat 配置构造（`_alignment_judge`，解析失败回退默认）。→ 本次 JJK 构建是**带设置跟随裁决器的首次生产规模对齐**。
- **连接测试假阴性**：内置 `connection-tests/vlm` 对 DeepSeek 返回 "DeepSeek不支持 vision_ocr"（该测试复用 vision OCR 能力位，deepseek manifest 无此能力）→ 不能作为 VLM 通道判据；实际通道由直连探测与应用构建双重验证（见 3.4）。

### 3.6 调优记录（18:26–18:32，用户要求）
1. **关闭 DeepSeek 思考模式**：settings `insight_vlm/deepseek` 的 openaiOptions.request.extra_body 设 `{"thinking":{"type":"disabled"}}`（rev 2→3）。DeepSeek 默认 thinking effort=high 且 reasoning 按输出计费；抽取任务下 reasoning 会先耗尽输出预算（探测 A：8,000 max_tokens 全为 reasoning、43.6s、0 内容）。
2. **图片压缩上限 1024**：同一 provider 行 imageMaxSize 0→1024（rev 1→2）。`vlm_client._prepare_image` 用 Pillow LANCZOS 缩最长边并重编码 JPEG q85（实测 190–279 KiB/页）。
3. **批内分片并发（新实现）**：引擎有 executionMode=parallel（jobs/worker_loop.py），但 Insight 硬编码 sequential（settings/resolver.py:1010），故在 `ProviderInsightAlgorithms.analyze_batch`（insight/worker.py）内实现：5 页批按 `INSIGHT_ANALYZE_PARALLEL`（默认 3，clamp 1..8）拆连续分片（2/2/1），asyncio.gather 并发 VLM 请求，分片独立校验、按原页序合并；单次 5 页调用链路与发布事务不变。质量取舍：分片内跨页实体约束弱化，由后续跨批对齐兜底。
- 实测：批耗时 30–35s → **≈5s**；吞吐 10.5 → **57 页/分钟**（≈5.4×）。

## 4. 阶段3：跨章节实体消歧观察（已填）

**本轮样本**：book `29011e06-5c43-41ab-905d-42891e2955de`（呪術廻戦 148 话/2998 页），run `bf374dbe-d391-4676-99ec-ac485f139399`，scope=chapter 前 10 话（234 页计划，224 页成功/10 页失败）。产物 `entity_alignment/default` rev1，**status=degraded**——由 `_publication_status`（derived.py:6131-6139）判定：源 run 状态为 `completed_with_errors` 即降级，与内容质量无关。

### 4.1 产物事实（`verify_acceptance6_alignment.py`，落盘 tmp_alignment_jjk.txt）
- 实体 252：characters 33 / items 34 / locations 59 / abilities 23 / concepts 103；别名映射 279；auto_merge_log 34（`llm_semantic_judge` 21、`location_detail_overlap` 10、`ability_core_match` 2、`character_alias_merge` 1），置信度全部 high，无空 raw/canonical 记录（旧版 None→None 缺陷未见）。
- ID 格式异常 0，`pinyin_` 回退 0；金额/日期类概念独立性通过（命中 1 个概念、重复 ID 0）。
- **valid_chapters 缺失复现**：252/252 全为空串（发现#2 在生产规模、真实章节窗口下同样复现）；章覆盖只能走「实体 pages → analysis_run_targets/pages → chapters」外部映射，脚本按此口径输出章覆盖。
- 抽取质量抽样（跨章节一致性成立的正面证据）：角色 Top10 与设定一致（虎杖悠仁 146、伏黑惠 114、五条悟 58、钉崎野蔷薇 54、两面宿傩 53、佐佐木 25…），概念 Top5 = 特级咒物/诅咒/式神/宿傩的容器/咒力 → 旧久留美金融题材提示词残留已清除（对应「提示词去题材化」改动）。

### 4.2 裁决漏斗与「否决率过高」定量
- 裁决缓存 `llm_entity_judge_cache.json`（父工作区，跨书共享）累计 **3013 对**：判「同实体」357、否决 2656（接受率 **11.8%**）；其中呪術相关 565 对，接受率 **9.4%**。
- 分类目接受率：characters 90/301=29.9%、items 65/193=33.7%、locations 124/850=14.6%、**concepts 78/1666=4.7%**、abilities 0/3。
- 候选入口（`src/backend_v2/insight/derived.py`）：粗筛为 `calculate_char_overlap_ratio` = `|chars1 ∩ chars2| / min(len1, len2)`（derived.py:202-207），**≥0.40 即送 LLM 裁决**（3486/3532/3581/3624/3664/3703）；`≥0.60` 由 rule 2.1 `substring_overlap` 直接合并、不问模型（3684-3696）；亲属/从属红线 `is_character_subordinate_or_rel_blocked` 把重合度 1.0 的关系对专门送进 LLM（因此必然被否）。
- 噪声候选实例：`items::两面宿傩的手指 :: 手机`（仅共享「手」字，min 分母 1/2=0.5 入选）、`items::特级咒物 :: 特级咒物（两面宿傩之指）`、`characters::虎杖倭助 :: 虎杖悠仁`（共享姓氏）。
- 成本：裁决阶段（23:03→23:48）596 次 LLM 请求、≈45 分钟，其中 88 次 502。

### 4.3 收紧候选的离线回放（用 3013 对缓存，零 token）
| 规则变体 | 保留真合并 | 丢掉真合并 | 砍掉无效调用 |
| --- | --- | --- | --- |
| R0 现状 min-overlap ≥ 0.40 | 352 | 5 | 53 |
| R1 max-overlap ≥ 0.50 | 212 | **145** | 1922 |
| R2 max-overlap ≥ 0.60 | 132 | **225** | 2400 |
| R5 concepts 用 0.75、其余 0.50 | 175 | 182 | 2251 |

→ **单纯提高字面阈值是亏本买卖**：真合并多为译名/同义/上下位（`电脑`↔`笔记本电脑`、`FX`↔`FX（外汇保证金交易）`、`久留美母亲`↔`久留美的母亲`），字面重合天然低；假候选多为共享姓氏或泛称前缀（`虎杖倭助`↔`虎杖悠仁`、`特级咒物`↔`特级咒物（…）`），字面重合高。字符集合重合度分不开这两类。

可直接落地的确定性拒绝（规则前置，零正确性风险，实测命中 false/true）：数值或日期不等 **379/1**；同前缀体系招式 `X·A` vs `X·B` **182/17**（17 例需人工确认，可能是同一招式异写）；关系/形态词只在一边（母亲/遗照/亡灵…）**83/4**；泛称 vs 限定名 **14/5**。
弱字面证据分布（供第二条召回通道使用）：无共同 2-gram 803 对（true 26）；仅共享 ≤1 字 687 对（true 21）。

### 4.4 结论
「候选太宽松」只是表象。根因有两条：①**候选与判据都只有字面**——裁决提示词只给类别 + 两个名字（derived.py:473-499），不含频次/出现页/描述，模型只能凭字面猜，对陌生专名一律否；②**失败语义把「未判定」写成「非同一实体」**——`judge_batch` 异常时整批写 False 并永久缓存（derived.py:520-524）。具体方案见 §6 发现 13-17。

### 4.5 整改实施与回放验收（2026-10-19，按 4 → 1 → 5 主线落地）
**方案 1 · 硬拒绝规则前置**（`src/backend_v2/insight/derived.py`）：新增 `hard_reject_pair(category, name1, name2)`，命中直接判「非同一实体」且不进 LLM——
- R1 数值/日期签名不等（`_digit_signature`，日期优先）；
- R2 同前缀体系招式 `X·A` vs `X·B`（仅 `abilities`/`concepts`；尾段同音或 `len(tail)>=2` 且编辑距离 ≤1 时豁免交 LLM）；
- R3 单侧关系/形态词（characters 走既有 `is_character_subordinate_or_rel_blocked`，其余类目用关系/形态后缀 + 前缀匹配；尊称同指如 `高木`/`高木老师` 豁免）；
- R4 泛称 vs 括号限定名（仅当短名命中 `GENERIC_BASE_TERMS`，如 `特级咒物` vs `特级咒物（两面宿傩之指）`）。
同时把粗筛换成 `is_judge_candidate(category, name1, name2, threshold)`：先硬拒绝 → 剥离泛词/虚词后缀后用**核心字**算重合度 → 核心名长度 ≤2 时禁用浮点阈值，要求子序列或同音变体（`釘子`/`钉子`、`浮亏`/`浮动亏损` 可过，`两面宿傩的手指`/`手机` 被拦）。全部 15 处粗筛点（characters/abilities/locations/items/concepts 的 c_name 与 alias、Rule 2.1 `0.60`、Rule 2.2、Rule 2.3 `0.35`）均改走该函数，硬拒绝也前置于确定性合并函数之前。
**方案 4 · 缓存与失败语义**：缓存按书隔离（`resolve_llm_judge_cache_file(book_id)` → `llm_entity_judge_cache.<book_id>.json`）；`judge_batch` 任何异常**不再写缓存**，只计数；保持布尔二态（不引入 inconclusive）；`_arbitrate_single` 失败返回 `None`（不再固化成 `same=false`）；写入改为 `.tmp` + `os.replace` 原子落盘；新增 `stats()` 观测（cache_hits/llm_calls/failures/unanswered/dropped）。
**方案 5 · 批量与并发**：`JUDGE_BATCH_SIZE` 默认 30（clamp ≤35）、`JUDGE_PARALLEL` 默认 3、`JUDGE_MAX_ATTEMPTS` 默认 3（均可用环境变量覆盖）；新增 `prefetch_alignment_judgements`，在归并前把所有通过闸门的候选对一次性批量判定并落缓存（原实现的 `judge_pair_sync` 是**每对一次 LLM 请求**，45 分钟的主因）；响应按 `id` 索引校验对齐，越界/重复/非整数一律丢弃，不再按位置盲目 zip。

**回放验收**（`tmp_replay_rules.py`，用 3013 对旧缓存，零 token）
| 指标 | 结果 |
| --- | --- |
| 不再送 LLM 的候选对 | **1154 / 3013（38.3%）** |
| 其中旧模型也判「否」（净省下的无效调用） | **1132** |
| 潜在误伤（旧判「是」被新规则拦掉） | **22（0.73%）**，姓名清单见 `tmp_replay_rules.txt` |
| 硬拒绝命中分布 | R1 379、R3 23、R4 18、R2 3 |
| 闸门（重合度/短名）拦下 | 731（短名保护 710、剥泛词后跌破阈值 50） |
| 已发布产物 34 条合并被新硬规则拒绝 | **0 条** |
| 现存别名对会被新规则拆掉 | **0 对**（不破坏已发布质量） |
| Rule 2.1 `substring_overlap` 救回的静默合并 | 0 条（该分支只对五大类目之外的类目生效） |

已知代价：无分隔符的 2 字形近字（`宿儸`/`宿儺`）不再进候选，需形近字表才能救回。

方案 5 的批次/索引/并发语义另用离线桩验证（`tmp_verify_judge_batching.py`，零网络，18 项全过）：75 对按 `batch_size=30` 切成 3 次请求（30/30/15）、峰值并发 = 3（等于 `parallel`）、响应乱序或含漏答/越界/重复/非整数 id 时仍按 id 正确映射（漏答不落盘、越界与重复丢弃、乱序不错位）、异常按 `JUDGE_MAX_ATTEMPTS=3` 指数退避重试且**绝不写缓存**、二次运行 75 对全部命中缓存（0 次 LLM 调用）、每书缓存文件互不相同。端到端「裁决阶段 45 分钟 → 5 分钟以内」这一条需网关恢复后用真实 run 计时确认（当前网关持续 502，无法在线复验）。

### 4.6 局部重跑暴露的两处副作用与修复（2026-10-19）

`retry-failed` 只补失败页（源 job `39c2c48f` → 重试 job `091d72b7`，只补 10 个失败页 + 6 个衍生步骤），这让新 run 自身缺少 layer 结果、且同一批页面在 `analysis_page_results` 里出现第二行，于是暴露出两处缺陷（发现 #24/#25）：

| 缺陷 | 机制 | 处置 |
| --- | --- | --- |
| 向量索引静默按 0 条事件发布 | `_layer_zero_event_records(frozen)` 只查本 run；重试 run 没有 `analysis_layer_results` → `vector_generations.event_count=0`、`eventTotal=0`，而 `coverage` 因 `expected_total = 页数 + 0` 仍算出 1.0 被当作成功发布；`qa/status.coverage.events` 随之从 152 掉到 0，global 问答失去事件召回 | **已修**：源 run 取不到 layer-0 时，回退到本书最近一次产出 layer-0 的 run（按 `analysis_runs.created_at` 倒序找首个有结果者）；`source_run_id=None` 的旧语义保持不变 |
| 对齐产物 `total_frequency` 翻倍 | 归并统计按 `analysis_page_results` 行累加，而 `pages` 已按页去重（虎杖悠仁 `total_frequency` 146 → 292，`pages` 仍 146；实体总数 252/别名 279/合并 34 均未变）。复现时 rev4 达 **448**（4 轮 × 112 页）vs 去重页数 156，**252/257 实体频次与页数不一致** | **已修（2026-10-19，用户拍板「按页去重」）**：见 §4.7 |

离线验证（`tmp_verify_vector_events.py`，零 token）：原始 run `bf374dbe` 自身有 layer → 152 条事件；重试 run `43bf699e` 自身无 layer → 回退后 152 条；不存在的 run → 同样回退 152 条；`source_run_id=None` → 保持 0 条。`python -m compileall -q src/backend_v2/insight/derived.py` exit 0；`tests_backend/v2/test_stage5_insight.py` = **4 failed / 148 passed**（181s；本轮修 `repository.py` 后由 5 降到 4，HEAD 基线为 5），失败项均为既有期望漂移，无新增回归。

遗留（已于 2026-10-19 网关恢复后消解）：已发布的那一代向量（generation 2）当时仍是 `event_count=0`，需重跑 vectors 步骤才会被改写——见 §5.4，重跑后 published generation 3 的 `coverage = {events: 160, pages: 234}`。

### 4.7 `total_frequency` 改为「按页去重」（2026-10-19，用户拍板）

**语义决策**：`total_frequency` 定义为**去重后的出现页数**（与 `pages` 同源同义），而非跨 run/跨修订的累加计数。理由：产物是「实体 → 出现页集合」的事实快照，同一页被重析两次不应等于出现两次；跨修订累加使数值随重跑次数漂移，无法作为频次排序依据（前端图谱、Top 榜、问答上下文都读它）。

**实现（两处，均 `src/backend_v2/insight/derived.py`）**
1. 统计累积循环之后新增「按页去重」块（约 3858 行）：`raw_stats` 的每个 `info` 先过滤掉 `None/""/0` 的非法页号，再 `info["pages"] = sorted(set(known_pages))`、`info["count"] = len(info["pages"])`；
2. 最终归一化循环（约 4312–4326 行、`final_payload` 组装前）再按同样规则重算一次：`known_pages` 过滤 → `ent["pages"] = sorted(set(known_pages))`、`ent["total_frequency"] = len(ent["pages"])`。**这一步是必需的**——`align_entities` 会以**已激活产物为基底**做增量归并（`existing_canonical_entities`，derived.py:3788-3799），历史产物里被撑大的 `total_frequency` 只有在这里才能被纠正。

**验证（重建对齐产物 + 复跑分析器）**
| 项 | 修复前（rev4） | 修复后（rev5） |
| --- | --- | --- |
| `total_frequency` ≠ 去重页数的实体 | **252 / 257** | **0 / 257** |
| 虎杖悠仁 | 448（页 156） | **156**（页 156） |
| 伏黑惠 / 钉崎野蔷薇 / 五条悟 / 两面宿傩 | 273 / 132 / 130 / 117 | **123 / 60 / 58 / 53** |
| 产物状态 | degraded | **ready**（rev5） |

分析器 `verify_acceptance6_alignment.py` 相应新增 [7] 不变量检查（`total_frequency == len(pages)` 全量比对）与 `run_id` 缺失时的回退（见发现 #26）。

**顺带确证（发现 #27）**：产物 `status` 由 `_publication_status`（derived.py:6650-6658）决定——`frozen/current` 任一侧的 `source_run_status == "completed_with_errors"` 即记 `degraded`。故手动重建的 entity_alignment 转 **ready**，而 compressed_context/overviews 仍 `degraded`：它们的 layer 结果来自那个含失败页的旧 run，**需重建才会转 ready**。


## 5. 阶段4：下游验证（已填）

### 5.1 本轮 job 结果
job `39c2c48f-3e56-4c43-a14f-e93e7d44a6e5`：**completed_with_errors**（14:34:03Z→15:48:51Z = 本地 22:34→23:48，74 分钟），items = **completed 229 / failed 15**。失败构成：10 个页面项（#145-154，`AI 流响应 choices 格式错误`，两批各 5 页，第二批 0.00s 级联）+ 5 个衍生项（#237-241：`汇总分组失败 2/9；成功分组已保存`、`汇总分组失败 1/1；成功分组已保存`、`API 错误 502` ×3）。
节奏：22:34 起页面阶段约 10 页/分钟（旧网关 gemini-3.8-flash-high，并发 1），22:58 页面基本收尾，23:03 起进入衍生；23:03 的 502 风暴打掉 4 个衍生步骤（汇总第 3 层分析 9.09s、整理全书上下文 8.72s（含 3 次重试）、生成无剧透概览 9.55s、生成剧情总结 8.84s），随后时间线+裁决阶段 596 请求/88 次 502。

### 5.2 三项验证脚本输出（落盘 tmp_alignment_jjk.txt / tmp_downstream_jjk.txt / tmp_qa_jjk.txt）
- **对齐**：见 §4.1。
- **图谱** `GET /api/v2/insight/graph`：节点 252 / 边 1302 / 孤立节点 0；类目分布与对齐一致；度数 Top 与频次一致（虎杖悠仁 度 190、伏黑惠 161、五条悟 94、钉崎野蔷薇 87、两面宿傩 71）；边权分布 {1:917, 2:173, 3:70, 4:42, 5:30}。
- **时间线** `GET /api/v2/insight/timeline`：事件 100、`requested_mode=enhanced` 但实际 `mode=simple`、status=degraded，`fallback_reason="API 错误 502:"`（derived.py:934 将 `mode != "enhanced"` 记为 degraded）；**人物条目 0**（`characterPage.totalCount=0`，simple 模式只保留 story_summary 事件列表）；页覆盖 150/2998（范围 1–160，即前 10 话有效页）。
- **概览** `GET /api/v2/insight/artifacts/overviews/no_spoiler` 与 `/story_summary`：**均 404**（对应 5.1 中 502 失败的衍生项，产物从未发布；两产物此前一轮 run 被取消时停留在 status=building、is_active=false）。
- **问答状态** `GET /api/v2/insight/qa/status`：`available=True, generation=1, reason=None`，coverage `{events: 152, pages: 224}`。
- **问答**（`verify_acceptance6_qa.py`，SSE）：
  - Q1 `exact`「虎杖悠仁为什么吃下了两面宿傩的手指？」：匹配实体 2（虎杖悠仁/两面宿傩的手指）、引用 7 条（带 pageNumber + chapterOrdinal）、16.5s / 12 chunks；答案有据且会拒答——第一根手指动机标注「资料未提供」，后续动机引第 71/76/91 页（爷爷遗言、拯救更多人、确立生死方式）。
  - Q2 `global`「少年院发生了什么？」：引用 8 条**全部 chapterOrdinal=1**、匹配实体 0、答案「证据不足」；而少年院场景位于第 6–8 话（对齐产物 `location_...少年院·第二宿舍内部异空间` 章覆盖 2/10，时间线亦有少年院事件）→ **global 模式召回未命中后段章节，待查**（发现#20）。

### 5.3 优先级 0：失败项局部重跑（266 行改动的验证）
- **能力本就存在**：`src/backend_v2/jobs/retry.py:170` `expected_status = "completed_with_errors" if failed_only else "failed"`，端点 `POST /api/v2/jobs/<id>/retry-failed`（`jobs/routes.py:258-278`，需 `Idempotency-Key`）就是为「对 `completed_with_errors` 任务只重跑失败项」设计的。
- **两个阻断性缺陷（已修）**：① `retry.py:660` 的 `job_plugin_snapshots` 在插件域清理时被误删 import → `NameError` 直接 500；② `src/backend_v2/insight/routes.py:475` 的 `LOGGER` 既未 import logging 也未定义。修复后 `retry-failed` 正常受理（job `091d72b7-241c-4f61-99d2-0f98668f766f`，20 项 = 10 个失败页 + 5 个失败衍生项 + 5 个被取消衍生项，`_reuse_book_results` 复用已发布页）。
- **重试通道改造**：`src/shared/openai_execution.py` 的业务重试由固定 `sleep(1)` 改为指数退避 `_business_retry_delay(attempt)`（1/2/4…上限 16s，±20% 抖动，`OPENAI_COMPATIBLE_RETRY_BASE_SECONDS`/`_CAP_SECONDS` 可调），日志可直接观测：`模型服务暂时不可用（HTTP 502）｜1.4 秒后重试｜第 1/3 次重试`。
- **本次结果**：重跑窗口内旧网关再次进入 502 全故障（00:20–00:23 连续 44 次 502、0 次成功），10 个页面项再次全失败（错误全为 `INSIGHT_BATCH_FAILED | API 错误 502: `），5 个衍生项在退避重试中 → **产物仍未发布**。
- **结论**：局部重跑链路与退避重试**已验证可用**（不再需要整轮重跑），本次发布失败的唯一原因是上游网关可用性。
- **2026-10-19 补跑（网关恢复后，发现 #18/#19 关闭）**：`POST /api/v2/jobs/091d72b7-241c-4f61-99d2-0f98668f766f/retry-failed` → 新 job `8431eb87` / 新 run `2c9d40c2`，**20/20 项 completed、234/234 页成功、0 失败**（12:38→12:54，约 16 分钟）。产物发布：`compressed_context/default` rev1、`overview/no_spoiler` rev1（293 字）、`overview/story_summary` rev1（535 字）、`entity_alignment` rev4→rev5；时间线从 `mode=simple / fallback_reason="API 错误 502:"` 变为 **`mode=enhanced`**；`qa/status` generation 3、`coverage={events:160, pages:234}`（事件数 0 → 160 正是发现 #24 的修复生效）。详见 §5.4。

### 5.4 完整复验（2026-10-19，三脚本全绿）

样本：旧 book `4c33cc46` 迁移出的 `29011e06-5c43-41ab-905d-42891e2955de`，已发布 234 页（前 10 话）+ 全部衍生。

| 脚本 | 结果 |
| --- | --- |
| `verify_acceptance6_alignment.py` | 257 实体 / 286 别名 / 36 条归并日志；characters 35、items 35、locations 60、abilities 23、concepts 104；**频次不变量 0/257 违例**；章覆盖恢复（虎杖悠仁 10/10、伏黑惠 10/10、钉崎野蔷薇 8/10、五条悟 7/10、两面宿傩 7/10） |
| `verify_acceptance6_downstream.py` | 图谱 **257 节点 / 1339 边 / 0 孤立**（类目分布与对齐一致；度 Top 与频次一致：虎杖悠仁 195、伏黑惠 167、五条悟 94、钉崎野蔷薇 91、两面宿傩 71；边权 {1:930, 2:184, 3:73, 4:48, 5:31}）；时间线 **events=5 / mode=enhanced**、人物条目 6、页覆盖 25/2998（范围 1–234）；概览 no_spoiler 293 字、story_summary 535 字（**均不再 404**，status=degraded 原因见发现 #27）；`qa/status` available=True、generation=3、coverage `{events:160, pages:234}` |
| `verify_acceptance6_qa.py`（SSE，`--mode exact`） | 问「虎杖悠仁为什么能压制宿傩？」：匹配实体 1（虎杖悠仁）、**引用 7 条**（带 pageNumber + chapterOrdinal）、11 chunks / 12.1s；答案分「事实依据 / 资料推断」并显式标注证据不足项（容器体质成因资料未说明） |

**同时完成的回归验证**：`tests_backend/v2/test_stage5_insight.py` = **4 failed / 148 passed**（178s，与改动前基线一致，无新增回归；4 条失败为既有期望漂移，明细见 `tmp_pytest_insight_dedupe.txt`）。

### 5.5 问答双模式合并为单一「通用」模式（2026-10-19，用户要求）

原实现把问答分成 `exact`（页面/向量检索，前端显示为「精确」）与 `global`（只喂全库产物）两条互斥通道：前者拿不到全局脉络，后者拿不到页面证据（发现 #20 即 global 未召回后段章节）。产物齐备后二者信息互补，故合并为一个通用模式。

**后端改动（3 文件）**
- `qa.py`：新增 `DEFAULT_QA_MODE="generic"`、`QA_MODES={generic,exact,global}`、`GENERIC_GLOBAL_CANDIDATE_LIMIT=2`；`_retrieve` 拆为 `_page_candidates`（向量检索 + 页面分析回退 + 父子上下文 + 抽取率监控）与 `_global_artifact_candidates`（只取全库产物），`generic = 全库材料(≤2) + 页面候选`，`_global_context` 退化为「全库或回退 20 页」的旧入口；`create` 对 `generic` 不再硬性要求「exact 可用」（只有 `exact` 保留旧报错），`mode` 默认 `generic`。
- `derived.py::qa_status`：接受 `generic`，与 `exact` 共用页面/向量可用性判定。
- `routes.py`：`GET /qa/status` 的 `mode` 允许 `generic` 且默认 `generic`；SSE 前置的重排只对页面候选执行，`type=global` 的候选**不参与重排并固定前置**，否则会被重排挤出 `topK`（实测重排输入 31 条 = 候选总数 33 − 2 条全库）。

**端到端实测**（13:33，栈内新代码，问题「虎杖悠仁为什么能压制宿傩？」，`--mode generic`）
- 检索步骤返回 `mode=generic`，候选首位即 `compressed_context:default` 与 `overview:story_summary`（`type=global`、`hybridScore=1.0`），其后才是向量召回的 event/page 候选。
- 重排 31 条 → 取 8 条；最终「参考资料 10 条」= 2 条全库材料 + 8 条页面证据，答案正文引用到「资料 1、2、10」——即全局脉络与页面证据被同一次回答使用。
- 生成 7.88s、端到端 11.8s / 8 chunks，答案保持「事实依据 / 证据不足说明」结构；`/qa/status?mode=generic` 返回 `available=true`、`generation=3`、`coverage={events:160, pages:234}`。
- 兼容：旧客户端继续传 `exact` / `global` 仍被接受（走各自旧通道），`generic` 为新的默认值。


### 5.6 产物 `degraded` 语义拆分：状态只按本次 run 的目标页判定（2026-10-19，用户拍板）

发现 #28 的根因是「用全书页数当分母」，于是只要分析范围小于整本书，派生产物就永远 `degraded`、重建也无法转 ready（实测 rev2 仍 degraded）。用户拍板采用拆分方案：**状态只按本次 run 的目标页判定，「只覆盖全书一部分」不再折算成失败**。

**代码改动（2 文件）**
- `derived.py`（原 3044-3051）：删除用全书页数比较并改写快照 `source_run_status` 的代码，快照直接采信 run 自身的 status 列（`source_run_status = active_run_status`，derived.py:3046）。
- `repository.py`（1944-1952）：`derived_status` 只看本次 run 的 `final_status`——`completed_with_errors` 已由本 run 的失败页数/后处理失败在 `finalize_run` 中判定（repository.py:1462-1464），不再与全书页数比较。
- 覆盖范围不再折算为「失败」，改由 `snapshot.pages`、`compressed_context` 的 `source_coverage`、`GET /qa/status` 的 `coverage {pages, events}` 与 run 的 `target_count` 表达。
- 已知取舍：`entity_alignment` 独立重建路径仍硬编码 `status="ready"`（derived.py:4354-4361）——其产物 `run_id` 为 NULL（发现 #26），没有 run 可判定；run 内 staged 路径由 `finalize_run` 按同一规则赋状态。

**测试同步（`tests_backend/v2/test_stage5_insight.py`）**
- 期望由 degraded 改为 ready：只分析 1 页时的 compressed_context（约 :688）、新页尚未分析时的衍生重建（约 :2147）、部分范围全局产物重建（用例更名 `test_partial_analysis_publishes_ready_global_artifacts`，约 :5476）。
- 删除一条过期断言 `'"characters"' not in payload`（约 :1093）：`analysis.knowledgeMode` 开启时页面产物会按 `page_schema._extra_entity_keys` 规范携带 characters/locations/abilities/items/concepts 五个实体数组，属规范字段而非原始模型字段。

**验证**：重启栈后对三个产物各触发一次重建（job `d7ba340e-9ca0-4045-910b-f23346ed335a` / `ddb49c59-3a57-4cf7-9ad7-741471920001` / `a13510df-373e-466b-9bf6-5204f48a8197`，均 completed 1/1）→ `compressed_context/default`、`overview/no_spoiler`、`overview/story_summary` 全部 **rev3 `status=ready`、active=True**（rev1/rev2 已停用）；`entity_alignment/default` rev5 保持 ready。此后 `degraded` 只表示该 run 自身确有失败页或后处理失败，补跑成功后重建即可转 ready。

## 6. 发现与建议汇总（滚动）

| # | 级别 | 发现 | 建议 |
| --- | --- | --- | --- |
| 1 | 高 | test-mode 孤儿 running job 死锁（3.3） | unified_launcher 崩溃重启时调用 reconcile；或 test Worker 注册时收敛旧 epoch 在途工作 |
| 2 | 中 | 对齐产物 valid_chapters 全空串（章归属丢失） | 生产调用点传入真实章节窗口或事后由 pages 反填 |
| 3 | 中 | 流式读无超时兜底 + 无 item 级租约 | 传输层加读超时；job 系统加 item 进度看门狗 |
| 4 | 低 | story_summary 阶段 502 无自动重试（run 内） | 阶段级重试或失败后允许单独重触发（端点已具备） |
| 5 | 低 | ChromaDB 声明为必装依赖但环境缺失 | 安装文档/启动自检 |
| 6 | 运维 | 运行栈上 pip 安装会锁 .pyd 且解析器升级依赖树 | 运维规程：--no-deps 钉版本安装 |
| 7 | 高 | 对齐语义裁决器硬编码网关，不跟随供应商设置（已修复，见 3.5） | 产线代码禁止硬编码提供商端点；置信度降级要可观测 |
| 8 | 低 | connection-tests/vlm 对 DeepSeek 假阴性（vision_ocr 能力位） | VLM 连接测试改测纯 chat 视觉能力 |
| 9 | 低 | full 范围重触发不跳过已发布页（每轮全量重析；部分范围才会复用 active 结果，commands.py:247-253 注释确认） | 如需故障续跑，可考虑"仅补失败页"范围；当前重触发成本=全量一遍 |
| 10 | 高 | DeepSeek 默认 thinking 模式在长 JSON 抽取下耗尽输出预算且 reasoning 按输出计费（单批 43.6s/8,000 tokens/0 内容） | 已通过 extra_body 关闭；供应商接入应显式声明思考开关并暴露在设置中 |
| 11 | 中 | 批量分析只能串行（Insight 硬编码 executionMode=sequential，5 页单请求） | 已实现批内分片并发（INSIGHT_ANALYZE_PARALLEL）；引擎级多 item 并行可作为后续优化 |
| 12 | 中 | 图片压缩上限默认 0（不压缩） | 建议默认 1024–1280 并按 provider 提示 |
| 13 | 高 | 对齐裁决候选对仅按字符集合重合度（交集/较短名长度 ≥0.40）粗筛：本 run 呪術样本 565 对接受率仅 9.4%（53），concepts 4.7%；离线回放证明提高该阈值会丢掉更多真合并（R2 保留 132/丢 225） | 不以字面阈值作为主要杠杆：改为「字面提议 + 向量/描述召回」双通道；字面通道换用最长公共子串/词法结构（子串、共享≥2 连续字、括号限定关系） |
| 14 | 高 | `judge_batch` 异常把整批判为 False 并写入永久缓存（derived.py:520-524），且缓存文件跨书共享（当前 3013 对中含旧书 key，路径解析 derived.py:385-400） | **已实施**（§4.5 方案 4）：异常不落缓存、缓存按 `book_id` 隔离、写入原子化；**不引入** inconclusive 第三态（未命中缓存天然会重判，保持布尔二态避免下游解析复杂化） |
| 15 | 中 | 裁决提示词无上下文，只给类别 + 两个名字（derived.py:473-499），模型只能凭字面猜 | 注入两侧实体的总频次、出现页/章、page 级 description/concept_types、已有别名簇，让判定基于共现/描述证据 |
| 16 | 中 | 概念类目接受率 4.7%（78/1666）远低于 characters 29.9%、items 33.7% | 阈值与规则按类目分档；concepts 先走数值/日期/专名确定性判据 |
| 17 | 低 | 裁决 `batch_size=15`（derived.py:461）且串行 → 596 请求/45 分钟 | **已实施**（§4.5 方案 5）：默认 30 对/请求（上限 35，兼顾长列表尾部注意力衰减与数组错位）、并发 3、响应按 id 索引校验；叠加盟并前批量预判（`prefetch_alignment_judgements`），把"每对一次请求"的 45 分钟压到分钟级 |
| 18 | 中 | 概览产物 no_spoiler / story_summary 在生产 run 中 404（衍生项 502 后未补跑） | 阶段级自动重试，或失败后单独重触发（端点已具备）；发布前校验必备产物齐备 |
| 19 | 中 | 时间线在生产 run 回退 simple（`requested_mode=enhanced`、`fallback_reason="API 错误 502:"`，derived.py:934），人物条目 0 | enhanced 失败应重试，并把降级原因显式呈现；simple 产物不应让「人物条目」看起来是 0 条 |
| 20 | 中 | 问答 global 模式未召回后段章节（问「少年院」返回第 1 话 8 条引用 + 证据不足，而少年院在第 6–8 话） | 排查 global 检索召回/切分与向量产物覆盖；exact 模式正常可作对照 |
| 21 | 中 | 流式响应中的空 choices 帧（SSE 内联错误帧）使整批 5 页失败（本 run 10 页；`_extract_stream_chunk` 仅容忍 `choices==[]`，src/shared/ai_transport.py:425-441） | 传输层把「无 choices 的错误帧」判为可重试错误，而非不可恢复的格式致命错误 |
| 22 | 低 | 页面 schema 强校验（CORE_PAGE_FIELDS 含 warnings，无默认值，insight/page_schema.py:115-119）导致模型漏字段时整批 5 页重试（本 run 第 55-58 页同类现象后恢复） | 可缺省字段给出默认值，或把校验失败降级为单页重试 |
| 23 | 中 | 清理插件域时遗漏「未定义全局名」：`jobs/retry.py:660` 的 `job_plugin_snapshots` 未导入、`insight/routes.py:475` 的 `LOGGER` 未定义 → `retry-failed` 直接 500（已修复，见 §5.3） | 把「未定义全局名」静态扫描（`tmp_scan_undefined.py`，基于 symtable）纳入 CI，防止"删域漏使用点"这类回归 |
| 24 | 高 | **局部重跑（retry-failed）后向量索引静默按 0 条事件发布**：新 run 自身没有 layer 结果，`_layer_zero_event_records(frozen)` 只查 `frozen.source_run_id` → `eventTotal=0`，而 `coverage` 仍报 1.0；同一现象使 `qa/status` 的 `coverage.events` 从 152 掉到 0（`vector_generations.event_count`，derived.py:5622-5631）。实测：原始 run 45 条 layer-0 / 224 页，重试 run 0 条 | **已修复**：源 run 无 layer-0 时回退到本书最近产出 layer-0 的 run（实测重试 run 恢复 152 条事件）；后续应把「向量覆盖率为 1.0 但事件数 0」列为发布前校验项 |
| 25 | 中 | 同一批页面被重跑后，对齐产物 `total_frequency` 翻倍（虎杖悠仁 146 → 292 → 448，`pages` 仍 146）：归并以已有产物为基底**累加**计数（`total_frequency += info["count"]`，derived.py:4186/4237/4254），而 `pages` 是去重并集 | **已实施（§4.7，按页去重）**：统计累积后与最终归一化各做一次 `pages=sorted(set(...))` + `total_frequency=len(pages)`（后者必需，才能纠正历史产物里被撑大的值）；复验 252/257 违例 → **0/257**。建议后续把「频次 == 去重页数」纳入产物发布前校验 |
| 26 | 低 | 手动重建（`POST /books/<id>/entity-alignment/rebuild`）产出的对齐产物 `run_id` 为 **NULL**，于是所有依赖「产物 run → 页 → 章」映射的展示（分析器/前端章覆盖）失去依据 | 重建时写入源 run（或按 `pages → analysis_page_results` 反查）；下游读者应容忍 NULL 并回退到最近 run（分析器已如此实现） |
| 27 | 低 | 产物 `status=degraded` 的判据是「source_run_status 含 `completed_with_errors`」（`_publication_status`，derived.py:6650-6658），且**不会因后来补跑成功而自动变绿**：`entity_alignment` 手动重建后转 ready，而 compressed_context/overviews 仍 degraded（其 layer 结果源自旧失败 run）（真实分母见 #28） | UI 应说明 degraded 的含义与消除方式（重建对应产物）；或让补跑成功后自动使下游衍生产物失效并排队重建。**已于 2026-10-19 按 #28 拆分语义：`degraded` 现仅表示本次 run 自身有失败（见 §5.6 / 决策 10）** |
| 28 | 高 | **产物「永远 degraded」的真实根因：派生状态把「全书页数」当分母**。局部范围 run（前 10 话 234 页，全书 2998 页）即使自身目标 234/234 全部成功，仍被判为「有失败」：①`derived.py:3044-3051` 用全书页数算 `source_run_status`（234 < 2998）→ 快照写 `completed_with_errors` → 经 `_publication_status`（derived.py:6650-6658）令产物 degraded；②`repository.py:1944-1952` 的 `derived_status` 同样用 `success_count < 全书页数`。因此**手动重建不可能转 ready**：2026-10-19 对 compressed_context / overview(no_spoiler) / overview(story_summary) 三个端点各重建一次，新 rev2 仍为 degraded（源 run `2c9d40c2` 运行状态确为 `completed`、目标 234/234 成功、`analysis_heads` 全部指向它）。口径也不一致：`entity_alignment` 的生成点硬编码 `status="ready"`（derived.py:4354-4361），同一批产物里只有它「干净」。测试 `test_stage5_insight.py:5474-5495` 把 degraded 当作期望行为 | 二选一：①**保留语义**（degraded ＝ 派生输入只覆盖全书一部分，信息量为真），由 UI 说明并在产物上显式展示 coverage；②**拆分「失败」与「覆盖不全」**：状态只按本次 run 的目标页判定（干净 run → ready），另在快照/payload 记录 coverage 与 partial 标志，同时统一 `entity_alignment` 口径并改那条测试。若不动代码，唯一能让产物真变 ready 的路径是跑完整本 2998 页。**已修复（2026-10-19，用户选方案②，见 §5.6 / 决策 10）**：删除 `derived.py` 快照里「全书页数当分母」的改写（改为采信 run 自身 status），`repository.py` 的 `derived_status` 只按本次 run 的 `final_status`；重建三个产物均 rev3 `status=ready`；测试期望同步 3 处、删除 1 条过期断言。保留取舍：`entity_alignment` 独立重建仍硬编码 ready（其产物无 run 可判定） |

## 7. 附录
- 监控手段：job_items 状态分布查询（tmp_job_status.py）、worker 日志（data-v2/logs/saber-worker.log）、守护脚本（tmp_guard_job.py，空闲 360s 触发 reconcile+continue）。
- 验收脚本：verify_acceptance6_alignment.py / verify_acceptance6_downstream.py / verify_acceptance6_qa.py（用法：`python verify_acceptance6_qa.py <bookId> "问题" [--mode exact|global]`）。
- 消歧分析脚本（§4 数据来源，均可重复运行、零 token）：tmp_judge_cache_stats.py（裁决缓存 true/false 分布与分类目统计）、tmp_candidate_rules_eval.py（候选规则 R0-R6 离线回放）、tmp_candidate_rules_eval2.py（确定性拒绝规则 P1-P4、弱字面证据分布）、**tmp_replay_rules.py（§4.5 整改后的规则回放：硬拒绝命中分布、误伤清单、已发布合并/别名回归）**、tmp_failed_and_artifacts.py（失败项与产物发布状态）、tmp_timeline_probe.py / tmp_timeline_probe2.py（时间线 mode/fallback_reason/人物条目）、**tmp_retry_failed.py（只重跑失败项）**、**tmp_scan_undefined.py（未定义全局名扫描）**。
- 运维/诊断脚本（§5.3、§5.4 数据来源）：tmp_inspect_retry_job.py（打印 job 状态 + items 逐项错误 + 冻结配置 vlm/chat/embedding + 产物行，`python tmp_inspect_retry_job.py <jobId>`）、tmp_artifact_detail.py（产物全列 + payload 键 + warnings）、tmp_wait_job.py（轮询至终态，`<jobId> <maxMinutes>`）、tmp_guard_job.py（空闲 360s 自动 reconcile + continue）、tmp_verify_vector_events.py（向量事件回退专项验证）。
- 落盘证据：tmp_alignment_jjk.txt、tmp_downstream_jjk.txt、tmp_qa_jjk.txt、tmp_judge_cache_stats.txt、tmp_candidate_rules_eval.txt、tmp_candidate_rules_eval2.txt、tmp_failed_and_artifacts.txt。
- 基线数据：kurumi_ch1_5_accumulation_report.txt（243 实体/329 别名/90 归并/LLM 缓存命中 685 vs 调用 123）。

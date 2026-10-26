# MiyakoKB（Saber-Translator-main）深度审计报告与优化演进方案

> 审计模式：**纯静态证据审计**。动态基线（pytest / 前端 Vitest / ACL 修复脚本）被宿主环境阻塞
> （PowerShell 进程初始化失败 0xC0000142 + 脚本执行策略拦截 PSSecurityException），
> 未获得任何运行时验证结果。本报告所有结论均标注证据文件与行号，运行时行为一律视为“待离线复现”。

审计对象：`Saber-Translator-main`（实际为 MiyakoKB 漫画知识库，version 0.1.0 / storageVersion 3.5.4）
审计约束：本地单用户优先；只出方案不实施；禁止真实模型/外部服务调用。
决策记录：`docs/decisions-saber-translator-audit.md`

---

## 一、已证实的问题（按严重度排序）

### P1-1 漫画导入：部分失败被系统性吞掉 + 重试不幂等

**证据（已复核确认）：**
- `src/backend_v2/manga_import/routes.py:189-318`：Venera 导入按 书/章/每50页 独立事务提交；单章/批次异常只归入 `details.failed`，整体仍返回 HTTP 200（`routes.py:379-414`）。
- `src/backend_v2/manga_import/local_confirm.py:19-290`：本地导入同构；`local_confirm.py:79,107` cover 发布失败被 `except Exception: pass` 吞掉。
- 每次导入为每书生成新 `uuid4` book_id（`routes.py:190`），无 draft/resume 机制 → **重试同一本书会产生残缺书 + 重复书并存**。
- 前端 `vue-frontend/src/components/bookshelf/MangaImportModal.vue:169-185` 只看 `res.imported`：`imported>0` 即弹“成功”并关闭；`imported=0` 只提示“查看控制台日志”。`details.failed` 与具体 error 对用户完全不可见。
- 测试缺口：`tests_backend/v2/test_manga_import_venera.py:1-192` 仅覆盖 library/cover/设置校验/目录穿越，**无任何部分失败或幂等用例**。

**影响（用户核心路径）：** 大批量导入是本产品第一入口，一次网络盘抖动/文件被占用即可静默产生残缺书籍，且用户无从得知哪些章节缺失，重试再制造重复书。

**修复方向（v0.1.x 止血）：**
1. 导入改为 draft（staging）+ confirm 两段式，或至少记录 per-chapter 状态使重试可识别“同一本书”并续传/跳过已完成章节（幂等键：源库 comic_id + chapter uuid + 内容指纹）。
2. 前端必须渲染 `details.failed` 列表（书名/章节/原因），`imported>0 && failed>0` 显示部分成功警告而非“成功”。
3. cover 发布失败不应 `pass`，至少标记 `cover_missing` 状态供 UI 呈现。
4. 补齐部分失败与幂等重试的 pytest 用例（复用现有只读 fixture 风格）。

### P1-2 错误契约不一致：扁平 `{error: string}` vs 前端结构化解析（全仓完整版）

**证据（契约子代理 fd35a723 复核，主线程抽查 `routes.py:344-350` 采信）：**
- `src/backend_v2/manga_import/routes.py:344-452` 各 400/500 返回 `{error: string}`，全仓 13 处；`/venera/library` 异常甚至返回 HTTP 200 + error 字段（`routes.py:344-350`）。
- `vue-frontend/src/api/client.ts:26-73` 只识别 `{error:{code,message,details}}` 对象形态，扁平字符串降级为通用 "Request failed"，具体原因丢失。
- `vue-frontend/src/api/mangaImport.ts:22` 把 `error?:string` 声明进正常响应类型。
- openapi 4 处声明矛盾（`v2.yaml:1694-1699, 1725, 1752-1757, 1782-1787` 声明 ErrorEnvelope，实现是扁平串），契约测试守不住。

**影响：** 所有 manga_import 端点的真实报错（路径非法、库损坏、设置缺失）在 UI 上全部表现为同一句笼统提示，排障只能靠后端日志。

**修复方向：** manga_import 全部错误改结构化对象（与 client.ts 对齐），`/venera/library` 异常不再 200；openapi 补全/修正声明；mangaImport.ts 类型修正；契约测试锁定。全仓其余蓝图已确认合规（content/jobs/settings/transfer/web_import/auth 均走 error_response）。

### P1 附随：契约面其他已证实缺陷（同批修复）

- **P2 错误码枚举漂移**：openapi `ErrorDetail` 枚举（`v2.yaml:6311-6340`，22 项）缺 15+ 后端实际 code（job_conflict/draft_locked/draft_conflict/resource_locked/asset_missing/provider_unavailable/authentication_required/csrf_failed/admin_required/auth_rate_limited/storage_upgrade_pending/forbidden/invalid_host/全套 auth code）；`generated/v2.ts:3806` 同步缺 → 前端类型 narrowing 失效。
- **P2 InsightConflict 语义污染**：insight 域约 250 处混用于乐观锁冲突（`continuation.py:384` 等）与业务前置校验（如 `character_skill.py:217`"还没有实体知识网络"），全部映射 409 revision_conflict，前端无法区分"别人改了"与"状态不对"。
- **P2 前端乐观锁缺口**：后端完整（`insight/routes.py:988-1104`、`continuation.py:384,1565-1591`），但全前端无 revision_conflict 处理，409 一律全局 toast 无重取/合并机制（baseRevision 手工传：`customAiProfileStore.ts:219,230`、`settings/index.ts:178`）。修复：client.ts 拦截 409+revision_conflict 发统一事件。
- **P3**：AssetQuotaExceeded usedBytes 等平铺在 error 对象（应入 details）。

### P2-1 历史任务查询 limit 不约束 interrupted 分支，可无界放大

**证据：**
- `src/backend_v2/jobs/repository.py:1057-1093`：limit 只作用于 `limited_batch_ids` 子查询；`or_` 引入的 interrupted 批次不受 limit 约束。
- `vue-frontend/src/stores/taskCenterStore.ts:159-217` 一次性拉取无 cursor，前端再 `trimHistoryBatches` 截 200 批（`:204-218`）；筛选只作用于已加载集（`:116-124`）→ 大量历史时既慢又筛选不准。

**修复方向：** interrupted 分支同样走 limit 子查询；前端历史改 cursor 分页（或至少明确“仅最近 N 批”的产品语义）。

### P2-2 Asset GC 崩溃窗口：unlink 在事务提交前执行

**证据：**
- `src/backend_v2/storage/assets.py:598-632`：immediate 事务内先 `path.unlink()` 再 delete 行。crash between unlink and commit → 回滚后 DB 行保留、文件已永久删除；`scan_integrity`（`:439-466`）只查存在性，下次维护才标 missing。
- 缓解因素（已核实）：候选必然 unreferenced（`:585-587,608-614` 事务内重查引用）+ 3600s grace，**不丢用户当前引用资产**，故降为 P2 而非数据丢失高危。
- 对照：`reconcile_orphan_objects`（`:468-527`）与 journal 恢复（`:413-437`）机制完整。

**修复方向：** 先提交“标记待删”事务，再 unlink，再提交删除（或 unlink 后写 journal 允许恢复）；Windows 单机用户 kill 进程概率不低，值得做。

### P2-3 一致性检查不校验对象内容完整性

**证据：** `src/backend_v2/storage/consistency.py:56-134` 只验证对象存在/状态、FK、向量集合存在，不校验 SHA256/byte_size。配合 P2-2 的窗口，磁盘损坏/误删可长期潜伏。

**修复方向：** 维护任务增加抽样（或全量、离线低峰）内容校验模式，复用资产表已存的 checksum 字段；结果写入 consistency 报告。

### P3-1 模型日志记录完整 prompt/文本

`src/shared/openai_execution.py:46` 起：默认 300s 超时合理；日志省略 Base64 图片但保留完整 prompt/回答文本。本地单用户下可接受，但含版权漫画内容与用户问答，公网多用户场景需脱敏/分级开关。**不列为 Bug，列为演进项。**

### P3-2 指纹不含内容哈希（未证实缺陷，保留观察）

`src/backend_v2/insight/derived.py:6477-6509` `_analysis_input_fingerprint` 只哈希 resultId/pageId/pageNumber/sourceChecksum/currentSourceChecksum，不直接哈希 summary/entities 内容。若 resultId 每次分析都是新 UUID 且更新不原地覆盖，则指纹等价有效；**未能静态完全证实 resultId 生命周期**，列为开放问题，待动态复现或补充阅读后裁决，不作为当前缺陷上报。

### P3-3 广谱 `except Exception: pass`

insight 面共 45 处，多数带 `noqa: BLE001` 注释且语义合理（词表缺失不阻断对齐、进度回调失败不影响裁决）。建议演进期做一轮“吞异常分级”：可吞的收敛到统一 helper 并记录 debug 日志，不可吞的（如 cover 发布、GC unlink）显式处理。**不逐处定罪。**

### P2-4 引用页码语义漂移（检索问答，已证实）

**证据（检索子代理 97568a89，主线程抽查 `qa.py:2004` 采信）：**
- `src/backend_v2/insight/qa.py:2004`：`page_num = page_ordinal if page_ordinal is not None else row["pageNumber"]`。
- `row["pageNumber"]` 一律是 run 范围全书页码（page_number_snapshot，`qa.py:882/917/1331/2149`），而 `pages.c.ordinal` 是章内序号（`schema.py:482`）。元数据查询成功时 pageNumber 被替换为章内序号，失败时（`qa.py:1991-1992` 仅 warning）静默回退全书页码——同一页面两种语义进同一字段。
- 模型上下文 `_location_label`/`_answer_context`（`qa.py:2235,2246`）教模型引用"全书第Z页"，citation 卡片却显示章内页码，用户看到的与点击的不一致。
- `QASaveNoteModal.vue:56` 用 citation.page 保存笔记，语义随引擎查询成败漂移进入持久化数据。

**影响：** 产品核心可信度——引用页码是知识库问答的价值锚点，语义漂移直接动摇"可验证"的承诺。

**修复方向：** `citations_for` 固定输出 `pageInChapter` + `globalPageNumber` 两个显式字段，不要"成功时替换、失败时回退"的单字段；前端 label 相应区分；元数据查询失败降级时显式标注"全书页码"。

### P2-5 客户端断开后模型请求不中止（白烧 token，已证实）

**证据：**
- `qa.py:1689-1744`（publish_chunk/on_chunk/before_request）、`routes.py:772-783`（GeneratorExit→finally cancelled.set()）。
- `cancelled`（threading.Event）只在 before_request 与下一个模型 chunk 到达时被感知。用户关页/切书后，producer 线程的 `asyncio.run(complete())` 仍在等待上游响应，无任何取消管道；模型思考期（reasoning 模型可几十秒无输出）上游请求跑完为止，上限 300s（`openai_execution.py:46`）。每次中途退出最多白烧一次完整回答的 token（实际烧多少需动态验证）。

**修复方向：** 把 cancelled.wait 包进协程（`asyncio.to_thread` 与上游请求 gather），断开时 cancel 上游 httpx 流。

### P3-4 generic 模式全局资料指纹失配被静默丢弃（已证实）

`qa.py:1451-1455`：fingerprint 失配直接 `continue`，不进 output 也不置 degraded。书籍重新分析后，QA 在 generic 模式下悄悄失去全局脉络（概要/压缩上下文），仅剩页面候选，用户无感知。修复：失配时记入 degraded reason（"全局资料已过期，仅基于页面证据回答"）。

### P3-5 rerank 失败导致整个 QA 失败，无降级（已证实，设计取舍建议复核）

`routes.py:674-683` + `qa.py:1509-1632`：rerank 异常（含重试用尽）直接冒泡→SSE error QA_FAILED。对比向量检索失败有完善的 lexical 降级+警告，rerank 无对称处理。修复：捕获后降级 `ranked=rankable[:topK]`（已有 hybridScore 排序）+ warning。

### P3-6 character_skill latest_run_id 按"页数最多"选 run（假设待动态验证）

`character_skill.py:337-352`：`order_by count(*) desc limit 1`。多 run 并存时（旧 run 全量 published、新 run 发布一半）角色档案/lorebook 的 page_evidence 基于旧 run，新 run 修正的角色信息不进档案。需确认多 run 并存是否常态后定级。修复方向：created_at 优先、页数仅 tie-break。

### P3-7 entity_alignment 使用无 status 过滤（假设待动态验证）

`character_skill.py:208-218`：只过滤 is_active 不过滤 status。若 alignment artifact 存在 stale 语义（同表 status 有 ready/degraded），旧对齐结果会被用于实体解析→实体错→evidence 页错。需确认 status 语义后定性。

---

## 一之二、知识管线（insight derived）面新发现（管线子代理 ccfc5883，主线程抽查采信）

**头号裁决：P3-2 开放问题关闭——指纹机制无缺陷（已证实）。**
`_analysis_input_fingerprint` 不会导致陈旧衍生产物：
- `copy_page_successes` 为复用页新建 UUID（`repository.py:1154`）；跨 run/换模型必换 resultId → 指纹必变。
- `publish_page_success` 同 run 同页 upsert 复用 resultId 原地覆写（`repository.py:1033-1043`），但只发生在 staging 期、派生步骤执行之前（`commands.py:57-71` 步骤顺序保证），snapshot 在其后构建。
- 双保险：每页发布即 `mark_book_insight_derived_stale`（`repository.py:1087→430-479`）置 stale，不依赖指纹比对。
- 页序/插页被 `current_page_number`（`derived.py:2464`）覆盖；独立派生 job 用当前 head 重建 snapshot 与冻结指纹比对（`derived.py:5665-5676`），不一致拒绝。
- 残留 P3（防御性）：指纹对 payload 内容盲（`derived.py:6477-6509`），安全依赖"publish_page_success 是唯一写入者且仅 staging 期"这一隐式合约。**建议 canonical 加入 payload_json SHA-256 作纵深防御**（成本极低）——未来任何"原地编辑已发布结果"功能都会静默破坏该合约。

**管线 P3 清单（9 项，均静态证实除 D 外）：**
- **A（重要）**：docstring 所述 `llm_entity_judge_cache.<book_id>.json` 缓存**不存在**（全仓仅 `derived.py:6009` 一行注释；`LLMBatchJudge` derived.py:620-661 无缓存逻辑）→ 每轮对齐重复支付全部裁决 token；docstring 误导维护者。修复：删失实注释或真实现（key 含 provider+model+api_key 指纹，tmp+rename 原子写）。
- **C（唯一数据质量缺陷，重要）**：`unmerge_alias` 拆分实体后 detached_entity 完整复制 `pages`（`derived.py:4264`）而 total_frequency 只减 detached_freq（`:4247`）→ 两实体声明相同 pages、违反自身 `total_frequency==len(pages)` invariant（`:4108-4113`）。UI 手工拆分别名即触发图谱/列表页共现失真。修复：alias_details 吙页码按页切分，至少清空 detached pages。
- **B**：`_GENERIC_CACHE`（`entity_alignment.py:106-130`）进程内永不失效，词表热改需重启。按 mtime 失效或文档化。
- **D（假设，仅非 SQLite 后端）**：revision 用 max+1 后 INSERT，`with_for_update` 被 `try/except: pass` 吞（`derived.py:3438-3441, 4209-4212`）；SQLite 写串行兜底无风险，换 PostgreSQL 并发可能重号。建议 `INSERT...SELECT coalesce(max,0)+1` 原子化。
- **E**：model_name 仅 `manifest.requires_model` 时强制（`commands.py:553-561`），空 model 使 `LLMBatchJudge.enabled=False`（`derived.py:636-640`）→ 实体对齐静默降级纯规则（仅 warning）。修复：chat 段 admission 强制 model 非空。
- **F**：`frozen_analyze_concurrency` 非法值静默回退 None（`provider_runtime.py:91-99`），`0` 不报错不生效；`commands.py:457-461` 报错文案漏 'english'。
- **G**：凭证版本被删后每批抛 JobConflict 逐批失败不快速失败（`worker.py:765-777`）。建议首错置 run failed。
- **H**：`_dispatch_entity_alignment_if_missing` 补偿派发失败仅 LOGGER.warning（`worker.py:762-763`），实体图谱缺失到下次分析 run 用户无感知。写 user_log error 或加"重建实体图谱"入口。
- **I**：`_validate_run`/`_publish_run` 失败留 staging/building 残留（`worker.py:638-650,708-720`），is_active=False 不可见、GC 可清，可接受。

**管线排除性确认**：publish_artifact/align_entities/unmerge_alias 同事务原子切换 active，partial unique index `uq_analysis_artifacts_active`（`schema.py:1359-1364`）+timeline_versions/vector_generations 同类索引（`:1398,:1471`）DB 层兜底；stale 时 revision+1 保单调（`repository.py:456-468`）；GC 只删 inactive+过 1h grace（`gc.py:139-151`），可达性根覆盖完整（`gc.py:162-215`）；worker 每页发布与 step 同事务、重试幂等、finalize completed_with_errors 无悬挂态；vector staged 校验 count+指纹（`repository.py:1796-1812`）。

---

## 一之三、存储面补充发现（存储子代理 5217b309，均 P3，主线程抽查采信）

- **F1**：本地 profile ZIP 导入无条目数/解压总大小上限（`image_import.py:42-43` 默认 None，仅 public 走 10000条/2GiB；`MAX_IMAGE_PIXELS` 本地=None `:31-35` 解压炸弹不设防）。修复：本地也套安全上限（可放宽数值）。
- **F2**：本地 manga_import 非原子（每页独立事务，crash 留部分书；批内孤儿资产依赖 GC 兜底【假设待动态验证】；publish_cover `except: pass` 静默吞 `local_confirm.py:74-108`）——与 P1-1 同族，并入其修复。
- **F3**：字体上传 `upload.stream.read()` 整读内存无上限（`settings/routes.py:443`；quota 仅 public 生效）。修复：流式+大小上限。
- **F4**：字体删除先删 DB 行再 unlink（`platform_repositories.py:1654-1655`），crash 窗口行回滚复活而文件已丢——与 P2-2 同类族，统一按"先标记后动手"模式修。

**存储排除性确认**：迁移元数据-only 快路径（`runner.py:231-238`）不可达且语义安全，3.5.0→3.5.1 资产迁移私有副本+file_hash+backup 兜底；zip-slip 全防护（`transfer/worker.py:439-496` 条目/总大小/压缩比 1000/symlink/'..'；`_scan_mobi :498-557` 越界）；取消→cancelled→gc 1h grace→maintenance 回收集合闭环无泄漏（实体对齐纯 DB 无文件副作用）；busy 重试无重复副作用（immediate_transaction + _assert_attempt fence + step 条件更新，`jobs/repository.py:3625-3813`；chapter_write_locks `:3990-3994`；容器导入每页复查锁 `transfer/worker.py:242-248`）。

---

## 一之四、认证与部署面 P3（契约子代理，主线程抽查采信）

- change-password 无 limiter（`auth/routes.py:203-214`；login/register/recover 均有）→ 已认证者可爆破旧密码。
- `account_disabled` 先于密码校验抛出 → 账号枚举（`repository.py:281-282`）。
- `openapi.json` 不在匿名白名单（`auth/http.py:12-20`），public 模式匿名 401。
- Secure cookie 纯 HTTP 误配陷阱：建议启动探测警告（public 强制 loopback+TLS 设计合理）。
- local profile 绑 0.0.0.0 时局域网无安全头无认证（`app.py:156-179` 给 lanUrl）——设计权衡，建议启动警告/文档明示。
- Waitress `trusted_proxy='*'`+count=1，多跳反代下 client_ip 可伪造（仅影响登录限速，P3，文档要求单跳）。
- 任务中心前端（SSE+reconcile+generation 防旧回调）复核健壮，无新增。

---

## 二、复核后排除的疑点（避免误报）

| 疑点 | 结论 |
|---|---|
| 向量 resume 未校验 generation/内容指纹 | **不成立**。`derived.py:1814-2053`：generation 隔离命名、非 resume 先删后建、resume get_or_create+upsert 幂等、覆盖率校验（:1953-1962）、orphan 按正则+DB 清理（:2026-2052）；同 job frozen snapshot 不变，checkpoint 数量+upsert 幂等足够。 |
| 指纹不含内容哈希导致陈旧衍生产物（原 P3-2 开放问题） | **已裁决：不成立**。resultId 生命周期静态证实：跨 run 必换 UUID（`repository.py:1154`）、staging 覆写先于派生（`commands.py:57-71`）、`mark_book_insight_derived_stale` 双保险（`repository.py:430-479`）。仅保留"payload 哈希纵深防御"为建议项。 |
| v-html XSS | **不成立**。`vue-frontend/src/utils/sanitizeHtml.ts:1-89` 白名单+URL 协议过滤。仍建议演进期以 CSP 兜底。 |
| 存储迁移框架 | **健壮**。`src/storage_migrator/runner.py:160-363` 状态机+file_hash 校验+空间预检+强制回滚；`lifecycle.py:29-97` staging 后 `os.replace`；元数据-only 快路径不可达且安全。无需修复。 |
| 密钥泄漏 | **未见路径**。redaction 正则+secret_values_from_json（jobs 日志各写路径）、auth `secrets.compare_digest`/`token_urlsafe(32)`、settings 拒绝 secret 字段；bootstrap os.environ.pop 用后即删。 |
| QA 瞬态请求 | **无缺陷**。`qa.py:580-709` token_hash 鉴权、busy 有限重试、close 在 immediate 事务内。 |
| Worker 线程池回收 | `worker_loop.py:1117-1141` 超时回收有 fence/看门狗兜底，静态未见泄漏；需动态验证后才能下结论。 |
| Axios/模型请求无超时 | **不成立**。client.ts 全局 300s；openai_execution.py 300s。 |
| 检索去重/跨书隔离 | **无问题**。record_id merge（`qa.py:1180-1339`）、pageId 去重（:1960）、seen 去重（:2135）；全链路 book_id/owner 过滤（`qa.py:389-395`）。 |
| exports 导出完整性 | **极严**。指纹校验链（head run+snapshot+artifact sha256+page identity+layer fingerprint）未发现绕过。 |
| 前端 QA 状态机 | **正确**。`QAPanel.vue:127-231` requestSequence+AbortController+三元守卫；`insight.ts:835-949` SSE 事件顺序严格校验。 |
| 取消/Chroma 集合泄漏 | **闭环**。取消→cancelled→gc 1h grace→maintenance 回收；实体对齐纯 DB 无文件副作用。 |
| busy 重试重复副作用 | **无**。副作用全在 immediate_transaction+fence+条件更新内（`jobs/repository.py:3625-3813`）。 |
| 页码 off-by-one | **不成立**。ordinal 均 >=1（`schema.py:469/503` CheckConstraint）。 |

---

## 三、演进方案（按版本迭代，遵循 用户优先级：正确性与数据安全 → 知识质量 → 性能 → 产品能力）

### v0.1.x —— 止血（1-2 周粒度）
1. **导入幂等 + 部分失败可见化**（P1-1 + 存储 F2）：draft/confirm 或幂等键续传；前端渲染 failed 明细；cover 失败不吞；补 pytest。
2. **错误契约统一**（P1-2 + 附随项）：manga_import 结构化 error；openapi ErrorDetail 枚举补全 15+ 缺失 code；InsightConflict 拆分语义（乐观锁 vs 前置校验）；mangaImport.ts 类型修正。
3. **引用页码双字段化**（P2-4）：citations_for 输出 pageInChapter+globalPageNumber，前端 label 区分，笔记持久化字段对齐。
4. **QA 断开取消管道**（P2-5）：cancelled.wait 进协程，断开 cancel 上游流。
5. **历史查询 limit 修复**（P2-1）；前端 409 统一拦截事件（P2 前端缺口）。
6. 建立可运行的本地动态基线（解决宿主 PowerShell 问题后跑全量门禁）。

### v0.2.x —— 稳定性
1. ~~GC 顺序反转消除崩溃窗口（P2-2 + 字体删除 F4，统一"先标记后动手"模式）+ integrity_status 即时更新。~~ **已完成**（`c78f89b`）：行先提交、文件后删，unlink 失败回滚行。
2. ~~一致性检查增加内容校验（抽样模式，P2-3）。~~ **已完成**（`d9cf031`）：默认抽样 32 个文件算 sha256，大小检查覆盖全部资产。
3. ~~**unmerge_alias 页面归属修复**（管线 C，唯一数据质量缺陷）~~ **已完成**（`c78f89b`）：两边各留至少一页且维持 `total_frequency == len(pages)`；payload 哈希纵深防御（指纹建议项）仍待做。
4. ~~rerank 降级 + generic 模式 degraded 透出（P3-4/P3-5）~~ **已完成**（`d9cf031`）：全局资料指纹失配区分「没有」与「已过期」，并走既有 degraded 通道透出；重排失败退回混合检索顺序。
5. ~~实体裁决缓存真实现或删失实 docstring（管线 A，节省重复 token）；空 model admission 强制（管线 E）。~~ **已完成**：`AlignmentJudgeCache` 按 `<data_root>/llm_entity_judge_cache.<book_id>.json` 落盘、key = sha256(system+prompt)、只缓存成功结果、只认同一 (provider, model, base_url, 密钥指纹)；chat 段 admission 改为要求 model 非空，不再静默退化成「无 LLM」。
6. ~~吞异常分级清理（P3-3）首轮；字体上传流式+上限（F3）；本地导入解压上限（F1）。~~ **已完成**：P3-3 首轮只给「完全无痕」的回退补 `LOGGER.debug`（实测 insight 面 58 处广谱 except，其中 25 处静默、33 处已有日志或把异常带进返回值，不是审计原文说的 45 处「多数带 noqa」）；字体上传改分块读 + 32 MB 上限；本地导入加有限解压上限（60,000 条目 / 8 GiB / 单文件 512 MB）与像素上限，不再等于「无上限」。
7. ~~前端历史 cursor 分页。~~ **已完成**：后端 `list_jobs` 支持 `after`，返回 `nextCursor`，`limit` 单位仍是批次；前端任务中心底部加「加载更早的任务」，按游标逐页追加并按 `jobId` 去重。

### v0.2.x 实施中发现的问题

- **批次游标窗口必须多取一批**：`_batch_before_cursor` 用 `<=` 把游标批次留在窗口内（否则同批次的兄弟行会被两边同时滤掉），
  于是外层 `_history_after_cursor` 必然整批排掉游标批次。窗口若只取 `limit` 个批次，每翻一页就少一整批，
  最后一批还会提前丢掉游标——5 个批次只能翻出 3 个。修法是带游标时取 `limit + 1`。
  这个缺陷在探针（手写分钟级间隔）里看不出来，只在连续 `create_batch`（毫秒级间隔）的测试里暴露。

### v0.3.x —— 知识质量
1. 裁决 P3-1：日志分级开关。
2. 检索质量评测集（离线，复用现有样本，禁止外部服务）。
3. 导入后自动完整性核对报告（章节数 vs 源库）。
4. character_skill latest_run_id 选择策略修正（P3-6）与 alignment status 过滤裁决（P3-7）——待动态验证后定级实施。
5. 认证面 P3 批处理：change-password limiter、账号枚举时序、openapi.json 白名单、HTTP 误配启动警告。

### v0.4.x+ —— 性能与能力
1. 大库导入/向量构建吞吐优化（已有 checkpoint/fence 基础，可加并行度配置）。
2. CSP 兜底、公网部署安全加固（次级目标）。
3. 新产品能力按需评估，均须写迁移回退与成本。

**兼容底线重申**：所有方案保留 SQLite 数据/对象资产迁移路径、Windows 单机启动、OpenAI 兼容接入；不引入常驻新基础设施；重建类变更必须附迁移回退与成本说明。

---

## 四、环境阻塞声明

- `python -m pytest tests_backend -q` 与前端 `pnpm test`：PowerShell 进程以 `3221225794`（0xC0000142）退出，无任何测试输出 → **未验证，不可宣称通过/失败**。
- ACL 诊断脚本：PSSecurityException（执行策略禁止），外层却返回 exit 0 → 同一脚本按规则不重试。
- 待离线复现项汇总：P1-1 实际重复书产生路径；P2-2/F4 崩溃窗口触发；P2-1 大历史量响应大小；P2-5 断开后实际 token 消耗；P3-6 多 run 并存是否常态；P3-7 alignment status 语义；存储 F2 批内孤儿资产 GC 兜底；worker 线程回收。

## 五、审计方法与证据等级说明

- 审计方式：主线程静态精读 + 4 个只读子代理分域（契约/存储/管线/检索问答）+ 主线程对关键发现逐条抽查行号复核（prove-it）。
- 所有"已证实"= 静态代码路径追踪结论（写入者唯一性、事务边界、job 步骤顺序、schema 约束均有行号证据）；"假设"= 需动态实验确认，未定罪。
- 四子代理原始报告全文已在审计会话中交付，本报告为去重合并后的最终版。

*报告完。等待用户确认后才实施任何修改。*

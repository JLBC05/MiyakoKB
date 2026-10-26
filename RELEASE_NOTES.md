# 发布说明

## v0.2.0

本版是 v0.1.1 之后的**稳定性版**：把审计报告里 `v0.2.x` 清单（共 7 项）全部落地，重点是「崩溃窗口、数据归属、降级可见性、成本与安全上限」，不引入新功能。

### 数据安全（v0.2.x-1 / -3）

- **资源回收不再可能丢文件**：垃圾回收与字体删除改为「行先提交、文件后删」，删除文件的 `unlink` 失败时把资产行原样插回（标记 `integrity_status = missing`，后续扫描可修复）。过去的顺序是先删文件再提交行，崩在两者之间就会留下「行已回滚、文件已丢」的永久损坏资产。
- **`unmerge_alias` 不再复制源实体的页面归属**：拆出的别名实体过去会整份继承源实体的 `pages`，导致两个实体声称出现在同一批页面上。现在按被拆走的频次切分页面，两边各留至少一页，并维持 `total_frequency == len(pages)` 的不变式。

### 一致性与问答（v0.2.x-2 / -4）

- 一致性检查新增**内容校验**：默认抽样 32 个资产做分块 sha256（`--content-sample N`，`0` 关闭，`-1` 全量），文件大小检查覆盖全部资产。此前只比对数据库里登记的大小，文件被替换成同样大小的内容不会被发现。
- 问答的全局资料**区分「没有」与「已过期」**：书籍重新分析后指纹失配的概览不再被静默跳过，而是以 `degraded` 通道透出（「全局概览资料与当前分析不一致…」）。
- 重排（rerank）服务不可用时**降级到混合检索顺序**并给出提示，不再让整个回答报错。

### 成本与安全上限（v0.2.x-5 / -6）

- **实体裁决缓存真正落地**：按书落盘 `llm_entity_judge_cache.<book_id>.json`，键为 `sha256(system + prompt)`，只缓存成功结果，只认同一个 `(provider, model, base_url, 密钥指纹)`。同一张候选图重跑不再重复计费；换供应商不会误用旧结论。此前 docstring 声称有这个缓存，实现里根本不存在。
- chat 段 admission 改为**要求模型名非空**：过去缺 `modelName` 时裁决器会静默退化成「没有 LLM」，实体对齐悄悄失去语义合并。
- 字体上传改为**分块读取 + 32 MB 上限**，不再把整个文件读进内存。
- 本地导入加**有限解压预算**（60,000 条目 / 8 GiB / 单文件 512 MB）与像素上限，不再等于「无上限」。
- 吞异常分级首轮：只给「完全无痕」的回退补 debug 日志（实测 insight 面 58 处广谱 `except`，其中 25 处静默）。

### 任务历史分页（v0.2.x-7）

- `GET /api/v2/jobs?scope=history` 支持 `after` 游标并返回 `nextCursor`，前端任务中心底部新增「加载更早的任务」。过去一次性只给最新 200 个批次，更早的历史在界面上永远看不到。
- `limit` 的单位仍然是**批次**而不是行（旧语义不变）。

### 工程

- 后端测试 **864 passed**（较 v0.1.1 的 840 增加 24 条）；前端 `vitest` **94 files / 675 tests**，`vue-tsc` / `eslint` / `stylelint` / UI 架构审计（含 `--audit`）全部通过；静态产物已重建。

### 已知遗留（不阻塞发布）

- 退役域的接缝：`/pages/{page_id}/bubbles` 等路由仍注册、契约仍有条目，但不再落库（返回 200 与空结果）。
- `plugin_agent` 半删除域、数据库悬空表（下一版 `3.5.5+`）仍未清理。
- 审计报告与决策记录按本次要求**不再随仓库分发**。

## v0.1.1

本版是 v0.1.0 之后的**止血版**，只处理审计报告（`v0.1.x` 清单）里会直接伤害数据或误导用户的问题，不引入新功能。

### 导入（P1-1 / P1-2）

- 漫画导入不再「要么全成功、要么整本失败」：单章目录缺失、页文件损坏、章序号重复或缺失都会记成一条带章/页定位的问题，其余章节照常导入，结果状态区分 `success` / `partial` / `failed`。
- 导入幂等：同一部漫画（Venera 用 comic id、本地用书名+章节签名）重复导入会续跑，已有章节跳过、缺页补齐，不再重复建书导致页序号唯一约束报错。v0.1.0 及更早导入的无标记同源书会被自动认领，不复制数据。
- 错误信封统一：导入相关的失败改为标准 `{"error": {"code", "message"}}`，Venera 库不可达返回 503 而不是 200 + 扁平错误字段；前端把每条问题渲染成「第N话（标题）第M页：原因［code］」。

### 问答引用（P2-4）

- 引用页码不再「成功时是章内页码、失败时是全书页码」的一段歧义数字：响应固定给出 `pageInChapter`、`globalPageNumber` 与 `pageNumberSource`（`chapter` / `run_global`）。章元数据查询失败时界面明确标注「全书第N页」，并落一条 warning，不再让用户以为是章内页码。
- 客户端断开后上游模型请求会被真正中止（过去的取消信号只在下一个分片到达时才被看到，关页面后仍会白跑一整个回答）。

### 任务历史（P2-1）

- `GET /api/v2/jobs?scope=history` 的 `limit` 现在同时约束 interrupted 任务；此前该分支不带上限，历史里攒下大批中断任务时每次翻页都会全量拉回。
- 前端新增统一冲突处理：任何 409 会广播一次 `miyako:conflict` 事件，任务中心据此重新拉取快照，不再出现「提示冲突但界面仍是旧数据」。

### 工程

- 发行包改为**单个 7z，不分卷**（`MiyakoKB-<版本>.7z`），原先把 `.7z` 切成 1900 MB 分卷的步骤已移除。
- 后端测试 **840 passed**；前端 `vitest` **94 files / 671 tests**、`vue-tsc` / `eslint` / `stylelint` / UI 架构审计（含 `--audit`）全部通过；静态产物已重建。

### 已知遗留（不阻塞发布）

- 退休域的接缝：`/pages/{page_id}/bubbles` 等路由仍注册、契约仍有条目，但已不再落库（返回 200 与空结果）。
- 数据库悬空表清理（下一版 `3.5.4+`）：plugin 集群残留仍在冻结契约中，线上均为 0 行。

## v0.1.0

本次发布的主线是**移除已退役功能域**与**恢复可发布状态**：翻译流水线、桌面端、插件域、本地模型推理等域在之前的重构中已停止使用，本次把它们的残留接口、配置、依赖、前端 UI 与测试一并清理，并补齐发布合规材料。

### 数据库

- storageVersion `3.5.2` → `3.5.3`（`src/storage_migrator/schemas/3.5.3.sql` + `src/storage_migrator/v3_5_3.py`）。
- 升级动作：退役 8 张不再使用的表；重建 `operations`、`page_assets`、`prompts`（去掉指向已删表的外键与索引，`prompts.type` 收窄为 7 个 insight 类型）；转换后执行外键完整性校验。
- 全新安装不再因契约与库结构不一致而初始化失败（此前的 3.5.2 契约比 metadata 多 8 张表，导致 `storage-migrator` 与受管启动器的存储准备全部不可用）。

### 后端

- 移除翻译流水线（译文行、气泡落库、渲染请求、字体快照、翻译约束等表与接口）、桌面壳、插件域（`/plugins*`、`/plugin-agent*`）、本地模型推理（检测/OCR/修复模型）相关域与其配置域。
- 修复的缺陷：
  - `GET /api/v2/system/capabilities` 500（读取已删除的 `profile.allow_plugins`）。
  - `PUT /api/v2/settings/transactions` 404（翻译设置缺 `proofreading` 段时触发 `KeyError`）。
  - `GET /api/v2/translation/bootstrap` 500（已删表的字段仍被读取）。
  - `qa_status` 与导出冻结校验：全库产物可用性与 knowledge-mode 页面载荷校验。
  - 字体共享目录 ACL 继承修复：此前只识别 `D:P(` 形态，实际系统产出 `D:PAI(`，导致修复永不生效。
  - 启动器默认路由：裸 `--role launcher` 不再落到不准备存储的 unified 启动器；新增 `--unified-launcher` 显式选择。
- 实体对齐：新增硬性前置拒绝规则（防止「特级咒物 / 特级咒物（两面宿傩之指）」这类被泛词剥离后合并）、语义裁决缓存按书隔离、裁决请求批量并发；LLM 裁判不再内置任何服务端地址与密钥，未配置时明确跳过并计数。
- 兼容层的业务重试改为指数退避 + 抖动。
- 问答：原 `exact` / `global` 两种模式合并为通用模式（页面候选检索 + 追加全库产物），旧值仍被接受并归一化。

### 前端

- 删除翻译、桌面端、插件 agent、本地模型相关的页面、组件、类型与死代码；同一批控件全部迁移到 UI 基元（`UiCheckbox` 新增 `indeterminate`）。UI 架构审计的债务预算归零。
- OpenAPI 契约补回遗漏的 import / insight 路由与类型，重新生成 `src/api/generated/v2.ts` 与静态产物（此前的生成物已过期，会让 CI 的 `check:api` 与生成物一致性检查失败）。

### 发布工程与合规

- **修复打包缺陷：产物此前缺少存储契约 SQL**（`app.spec` 未收集 `src/storage_migrator/schemas`），打包版在全新数据目录上无法初始化存储；现已补入，并把 `LICENSE`、`THIRD_PARTY_LICENSES.md`、`RELEASE_NOTES.md` 一并打进产物。
- 依赖清单移除 torch / torchvision / onnxruntime / 本地模型推理栈；CI 不再安装这些包，并移除已失效的模型搬运开关。
- 打包字体目录只保留三款自由许可字体（思源黑体、NotoSans-Medium、ChisugaShotai），其余 25 个无自由再分发许可的字体不再随仓库分发；第三方许可清单见 `THIRD_PARTY_LICENSES.md`。
- README 重写为 MiyakoKB 定位（导入 / 分析 / 实体对齐 / 图谱 / 问答 / 部署），移除作者个人赞赏码图片与翻译时代的过时截图、重复 logo；品牌名在对外可见层（启动横幅、打包产物名与入口 `MiyakoKB.exe`、发行资产名、README 与前端文案）统一为 MiyakoKB。内部标识保持不变以免牵动数据与运维接口：数据根 `data-v2/`、数据库文件 `saber.sqlite3`、`SABER_*` 环境变量、`saber_yolo` 模型键与 `saber_v2.py` 入口。**例外**：产品日志名已改为 `miyako-<role>.log` / `miyako-<role>-diagnostic.log`，旧的 `saber-*.log` 不会自动重命名（历史日志可自行删除）；README 补齐「来源与致谢」（派生自 Saber-Translator，GPL-3.0）与排障指引。
- 后端测试套件 **937 passed**；前端 `vitest` / `vue-tsc` / `eslint` / `stylelint` / UI 架构审计全部通过；打包后按 `probe_package.py` 逐角色（launcher / api / worker / storage-migrator）验证通过。
- 前端单测清理：随翻译/OCR 域退休而失效的用例（`promptMode.property.ts`、`settings.defaults.test.ts`、`settings.current-schema.test.ts`）已删除，`router.property.ts`、`settingsStore.property.ts` 按当前路由与 store 面重写，新增 `settings.backend-loading.test.ts` 覆盖后端优先加载与事务保存；全量 vitest **88 files / 642 tests** 全绿（此前的分目录运行掩盖了这批失败）。

### 已知遗留（不阻塞发布）

- **退休域的接缝**：`/pages/{page_id}/bubbles` 等路由仍注册、契约仍有条目，但已不再落库（调用会得到 200 与空结果）。是否把这些接缝一并裁剪（或改为明确的 410）待产品拍板。
- `plugin_agent` 作业类型、`page_repair` 操作类型等「半删除域」：数据库 CHECK 约束与既有行仍保留，前端设置界面是否彻底移除待一并决定。
- **数据库悬空表清理（`3.5.4`）**：4 张只被 `schema.py` 引用的表（`operation_asset_inputs`、`operation_credential_snapshots`、`operation_plugin_snapshots`、`studio_message_assets`）与 plugin 集群 / durable-operations 残留（线上均为 0 行）仍在冻结契约中。删除需要一次存储版本迁移并改动少量活代码，本轮判定发布前不做，已登记为下一版清理项（见 `docs/legacy-cleanup-report.md` §10）。
- **发行包形态**：CI 仍按 `cpu` / `gpu` 两个 variant 各出一份包，但两者依赖清单已逐字节相同（本地模型与 CUDA 栈移除后内容一致），是否收敛为单一发行包待定。
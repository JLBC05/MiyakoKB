# 发布说明

## v3.5.17（准备中，尚未打 tag）

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
- README 重写为 MiyakoKB 定位（导入 / 分析 / 实体对齐 / 图谱 / 问答 / 部署），移除作者个人赞赏码图片与翻译时代的过时截图、重复 logo；品牌名在对外可见层（窗口标题、打包产物名与入口、发行资产名、日志文件、环境变量）统一为 MiyakoKB（后端 Python 包路径保持不变）。
- 后端测试套件 **937 passed**；前端 `vitest` / `vue-tsc` / `eslint` / `stylelint` / UI 架构审计全部通过；打包后按 `probe_package.py` 逐角色（launcher / api / worker / storage-migrator）验证通过。
- 前端单测清理：随翻译/OCR 域退休而失效的用例（`promptMode.property.ts`、`settings.defaults.test.ts`、`settings.current-schema.test.ts`）已删除，`router.property.ts`、`settingsStore.property.ts` 按当前路由与 store 面重写，新增 `settings.backend-loading.test.ts` 覆盖后端优先加载与事务保存；全量 vitest **88 files / 642 tests** 全绿（此前的分目录运行掩盖了这批失败）。

### 已知遗留（不阻塞发布）

- **退休域的接缝**：`/pages/{page_id}/bubbles` 等路由仍注册、契约仍有条目，但已不再落库（调用会得到 200 与空结果）。是否把这些接缝一并裁剪（或改为明确的 410）待产品拍板。
- `plugin_agent` 作业类型、`page_repair` 操作类型等「半删除域」：数据库 CHECK 约束与既有行仍保留，前端设置界面是否彻底移除待一并决定。
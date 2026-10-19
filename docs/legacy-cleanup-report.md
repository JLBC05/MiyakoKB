# 原项目遗留清理报告（Saber-Translator → MiyakoKB）

> 日期：2026-10-18
> 范围：`D:\My_Doc\Mine\manga-knowledge\Saber-Translator-main`（`src/`、`vue-frontend/`、`openapi/`、`app.spec`、`requirements-*.txt`、仓库根）
> 目标：这份代码库已从「漫画翻译工具」重构为「漫画知识库（MiyakoKB）」。本报告记录**翻译时代遗留物**的全量扫查结果与处置，使仓库只剩下知识库链路真正需要的东西。

## 1. 方法

- **四路只读审计**（子代理并行，禁止修改文件）：①后端域级 `src/backend_v2/**`；②前端 `vue-frontend/**`；③数据/配置/常量/OpenAPI；④`src/core` + `src/shared` + 根脚本。
- 每条结论要求给出「路径 | 类型 | **真实执行的证据** | 引用方 | 建议（删除/保留/存疑）| 风险」，不确定一律标「存疑」，禁止猜测。
- 判定标准：①零生产引用（**仅测试引用不算引用**）②前端无入口 ③翻译流水线语义（OCR/检测/嵌字/抹字/翻译任务）④打包/契约/数据库死物。
- 处置原则：**高置信删除**；保留数据库 CHECK 约束与既有表、legacy job kind 的显式兼容桩、provider capability 映射表、以及前端仍在调用的端点。

## 2. 死代码

| 项 | 证据 | 处置 |
|---|---|---|
| `src/backend_v2/{studio,translation,rendering,desktop,browser_extension}/` | 源码已删，各目录只剩 `__pycache__`（合计 34 个 `.pyc`、`.py` 计数 0）；`src/**` 零引用 | **已删**（本次） |
| 根 `browser-extension/`（71 文件，含 `src/studio/TranslationView.vue`） | 其服务端域源码已删，`app.py` 中 `browser_blueprint = None` 且从不赋值 | **已删**（本次） |
| `src/core` 翻译簇 `.pyc`（ocr / detection / large_image_detection / color_extractor / translation / ocr_hybrid_manga_48） | `.py` 已删，`__pycache__` 残留 12 个 `.pyc` | **已删**（本次，共清 23 个 `__pycache__`） |
| `src/backend_v2/operations/**`（executor / repair / repository / routes，含 `step_kind="live_render"`、`page_repair`） | 4 条路由；前端 `operations.ts` 唯一 importer 是已死的 `characterStudio.ts`；语义为翻译时代的抹字/修图（LaMA） | **已删**（本次，用户确认） |
| `src/backend_v2/plugins/**` + `plugin-agent`（contract 39KB / runtime / package / repository / snapshots / agent / agent_worker / agent_tools + 路由 14 条） | 钩子步骤表 `HOOK_STEPS=(job,pipeline,detect,ocr,color,translate,ai_translate,inpaint,render)`、`PLUGIN_MODES=(standard,hq,proofread,remove_text)`；随包 3 个示例插件 `supported_steps` 全是 ocr/translate/detect/color/inpaint/render；前端 `@/api/plugin`、`@/api/pluginAgent` 零命中 | **已删**（本次，用户确认）+ 根 `plugins/` 示例插件 |
| `src/core/inpainting.py`、`src/core/bubble_geometry.py` | 唯一消费者是 `operations/repair.py`（+测试） | **已删**（本次，随 operations） |
| `src/backend_v2/local_models.py` 及其消费链（`dispatch.py`、`worker/model_lifecycle.py`、`worker/entrypoint.py`、`launcher/entrypoint.py`、`api/system_routes.py` 的 `/system/release-models`） | 常驻本地模型 = OCR/检测/LaMA 共 11 个 id；其依赖的 `src.interfaces.*`、`src.utils.*` 已删 → 实际已不可用 | **已删**（本次） |
| `api/app.py` 的 `browser_blueprint = None` 死分支 + `ApiSettings.browser_extension_*` | 该 blueprint 被赋值前恒为 `None`；`/browser-credentials/*`（`auth/routes.py`）唯一消费者是已删扩展 | **已删**（本次） |
| `jobs/retry.py` 的 translation/detect/text_import/style_apply 分支 | 已是显式桩 `raise NotImplementedError("…not supported in knowledge-only mode")` | **保留**：作为既有数据库里 legacy job 的显式兼容错误，优于静默走 `_clone_original` |

## 3. 死数据（数据库）

- `prompts` 表死行 `type='question_decompose'`（id `baa9bd8e-…`）：`schema.py:123-131 PROMPT_TYPES` 白名单 7 值不含它，`src/` 与 `vue-frontend/` 全仓零引用，仅历史迁移 SQL 提及 → **已删除**（用 schema 真实表对象，删除前后 1 → 0，剩余 7 行与白名单一一对应）。
- `data-v2/saber.sqlite3` 共 75 张表，**50 张 0 行**；`schema.py` 声明的 75 个 Table 与磁盘 1:1。零生产引用表 2 张：`job_step_asset_outputs`、`studio_message_assets`。
- **保留**：所有表结构不删（删表需要写迁移 SQL 并处理既有库 CHECK 约束，风险大于收益）。仅清掉零引用的死行。

## 4. 死配置与题材残留

- `derived.py` 实体裁决提示词里的**题材示例**仍写金融作品内容（「保证金维持率 / 保證金維持率」「爆仓 / 強制平倉」「FX 超入门指导书」）→ **已改成与作品无关的通用表述**（「某专业入门书籍」等）。页面分析提示词的示例此前已在 `worker.py` 去作品化。
- `entity_slug_map.json` / `alias_seed.json`：作品专属配置，前次清理已移出仓库（本次确认仓库内不存在）。
- 保留：前端 provider 字段标签表 `{insight_vlm:[modelName,customBaseUrl,openaiOptions,imageMaxSize]}`（活）、`PROMPT_TYPES` 7 值白名单、`DEFAULT_INSIGHT_SETTINGS` 的 16 个 provider 映射（与 `ai_provider_manifest.json` 一致，无悬空 provider）。

## 5. 契约层（OpenAPI）

- `openapi/v2.yaml` 原有 **215 条路径，其中 78 条后端零实现**：46 条 `/browser-extension/*`、24 条 `/studio/*`、8 条翻译时代 job 路径（`/chapters/{chapter_id}/{translation-jobs,translation-batches,remove-text-jobs,detect-jobs,style-apply-jobs,text-export,text-import/preview,text-import/commit}`）。
- 该文件经 `/api/v2/openapi.json` 下发并生成 `vue-frontend/src/api/generated/v2.ts`，删除后必须重新生成客户端类型。
- 后端实际注册路由 181 条（`api/app.py` 357-504 行 15 个 blueprint）。

## 6. 打包与依赖

- `app.spec` 原有三处**必然导致打包失败**的引用：`src/backend_v2/desktop/assets`（目录不存在）、`src/utils/image_rearrange`（模块已删）、`desktop/assets/app-icon.ico`（图标不存在）；`critical_packages` 里 `manga_ocr`/`rapidocr`/`ultralytics` 未安装，`find_spec` 直接 `raise ModuleNotFoundError`。→ **已重写**为知识库版：`critical_packages=['chromadb']`，排除 `torch/torchvision/transformers/rapidocr/manga_ocr/ultralytics/litelama/fugashi/unidic_lite`，图标改用现存 `static/favicon.ico`，并对 `onnxruntime` 元数据做存在性判断。
- `requirements-cpu.txt` / `requirements-gpu.txt` 仍是翻译栈（torch、OCR、LaMA…）→ **已按实际 import 重写**。

## 7. 前端

- `vue-frontend/src/router/index.ts` 只有 10 条真实路由：login / register / recover / account / admin / bookshelf / reader / bookDetail / graph / qa / settings。
- 死 cluster（`@/api/v2/webImport`、`@/api/plugin`、`@/api/pluginAgent`、`@/api/v2/operations`、`characterStudio`、studio/translation 视图与 store）→ **已删除**（含 barrel 导出清理）。

## 8. 保留项与理由

| 保留 | 理由 |
|---|---|
| `web_import` 全域（8 路由 + worker） | 语义是「网页漫画导入」，属知识库功能；本轮只清理其死前端残留（用户裁定保留后端） |
| `content` 的翻译时代路由（bubbles、quick-workspace、render-status 等） | 用户裁定保留；其中 `GET /translation-constraints` 被前端 `bookshelf.ts` 的 `getBookDetail` 实际调用 |
| `transfer` 的导出（zip/cbz/pdf） | 前端书架/详情页在用 |
| `continuation` 全域 | 知识库下游创作，与翻译无关 |
| DB 表结构、`JOB_KINDS`/`OPERATION_KINDS` 枚举、plugin/studio 悬空表 | 既有数据库兼容，删除需迁移 SQL |
| provider capability 映射中的 `plugin_agent` 项 | 既有 provider_settings 行仍存在，保留映射避免解析报错 |
| `docs/insight-cleanup-plan.md` | 前次知识库链路清理的方案与偏差记录，有历史价值 |

## 9. 验证

（2026-10-18 至 10-19 实测）

| 验证项 | 命令 / 方法 | 结果 |
|---|---|---|
| 模块编译 | `python -m compileall -q src/backend_v2 src/core src/shared` | exit 0（`src/interfaces` 已整体删除，不再参与） |
| 打包脚本语法 | `python -m py_compile app.spec` | exit 0 |
| `app.spec` 路径引用 | 逐条核对 datas / icon 源路径 | 全部存在：`src/shared/text_style_defaults_factory.json`、`src/shared/prompt_defaults_factory.json`、`src/shared/ai_provider_manifest.json`、`src/backend_v2/static/favicon.ico`；第 207 行的 `plugin.json` 是运行时目录探测（判断候选目录是否为插件），不是打包文件 |
| 服务启动 + 健康检查 | `python run.py --no-browser` → `GET /health` | 200 `{"status":"ok","role":"api","storageVersion":"3.5.2",...}` |
| 知识库关键端点 | 验收点 6 全链路实测（148 话 / 2998 页导入 → 全量构建 → graph / timeline / overviews / qa_status / QA 问答） | 通过，证据与数值见 `acceptance6_stress_report.md` §2-§5 |
| 后端测试收集 | `python -m pytest tests_backend --collect-only -q` | **969 tests collected，0 collection error**（原先 29 个测试文件 import 已删域模块的问题已清理完毕） |
| 后端测试全量运行 | `python -m pytest tests_backend -q -p no:cacheprovider` | **937 passed / 0 failed / 0 error**（849.94s，含 96 subtests passed；余为 skip）。存量腐烂已按域处置：存储契约升级到 3.5.3（`test_storage_migrator` 53 passed）、翻译时代用例改用冻结夹具 `tests_backend/v2/legacy_translation_defaults.py` 并逐条裁定、退休域（bubble 落库/翻译约束/渲染状态）用例改为钉住现状、ACL 用例显式设置 ACL、启动器路由回归修复（`test_role_boundaries` 19 passed） |
| 依赖清单 | 检查 `requirements-cpu.txt` / `requirements-gpu.txt` | PySide6 及其注释已删（唯一使用方 `tests_backend/v2/test_desktop_storage_preparation.py` 已随桌面域删除）；pytest 仍保持注释状态 |
| 前端生成类型重建 | `openapi-typescript ../openapi/v2.yaml -o src/api/generated/v2.ts`（7.13.0） | 通过：411054 → 281071 字节；重建后残留的 `browser_extension` / `web_import` 命中全部是**枚举字面量**（`enum: [global, browser_extension]`、`web_import_agent`、`web_import_commit`，属保留能力位），已无已删模块的类型定义 |
| 前端类型检查 | `vue-tsc -b --pretty false` | **0 条错误**（25 → 23 → 0：图标名/按钮变体白名单与 `stores/settings` 事务类型漂移均已修） |
| 未定义全局名扫描 | `python tmp_scan_undefined.py`（symtable 静态扫描 `src/`） | 空结果（修掉 `jobs/retry.py` 的 `job_plugin_snapshots`、`insight/routes.py` 的 `LOGGER` 之后） |
| 失败项局部重跑链路 | `POST /api/v2/jobs/<id>/retry-failed`（需 Idempotency-Key） | 补跑 **20/20 全部发布**（含 10 失败页、5 失败衍生、5 取消衍生），`degraded` 产物全部转 `ready`，详见 `acceptance6_stress_report.md` §5.3-§5.4 |
| 前端六项门禁 | `vitest run` / `vue-tsc -b` / `eslint .` / `stylelint 'src/**/*.{vue,css}'` / `check-ui-architecture.mjs` / `… --audit` | **全部 exit 0**（vitest：75 files / 587 tests passed） |
| 打包产物实跑 | `python -m PyInstaller app.spec --clean` + `.github/scripts/probe_package.py` | 首次探测失败（产物缺存储契约 SQL，见发现 #33）→ 修复 spec 后 **launcher/api/worker/storage-migrator upgrade+check 全部 ready，exit 0** |

未做 / 待补：

- **品牌定稿**（唯一未完成的产品决策项）：`app.spec` 产物名、数据目录/库文件名（`saber.sqlite3`）、日志前缀、前端若干字符串仍为 `Saber-Translator` / `saber`（tracked 文件 `-i saber` 共 431 处：tests_backend 159、src 121、其他 90、vue-frontend 39、.github 11、docs 11）。改名会牵动发行资产名（`Saber-Translator-<version>-<variant>.7z`）、打包契约测试与既有用户的数据目录，需单独拍板。
- README 内两张作者个人赞赏码（`pic/wechat_qrcode.png`、`pic/alipay_qrcode.png`，README 第 441/447/451 行）未处理——属个人信息，需作者决定删除或替换。
- `docs/legacy-cleanup-report.md`、`docs/insight-cleanup-plan.md` 原先被 `.gitignore` 的 `docs/*` 规则排除；本轮已加白名单使其可入库。

## 10. 未处理 / 后续建议

- **数据库层**：`job_step_asset_outputs`、`studio_message_assets` 等 0 行表仍在 schema 中；如需彻底清理，应写一版迁移 SQL 并同步 `schema.py`、`schema_metadata` 指纹与 `storage_migrator/schemas/`（本轮已按此流程把 8 张已退休表从契约与 metadata 中移除，见 3.5.3 迁移）。
- **退休域接缝**：`/pages/<id>/bubbles` 系列路由与契约条目仍在，但落库已由 `12c37e6` 改为 no-op（`existing_rows = []`）——「200 但什么都不落库」是否裁剪需产品拍板（HANDOFF 坑 12）。
- **`tests_backend/`**：已清理完毕（969 项收集 0 错误、937 passed / 0 failed / 0 error）；已删除三个随域退出的打包/桌面/模型搬运测试文件。
- **`docs/`**：`storage-audit-report.md`、`storage-migrations.md` 含翻译时代表说明，可后续修订。

# 第三方组件许可

本仓库自身以 **GNU GPL-3.0** 发布（见 `LICENSE`）。下列第三方组件随本仓库源码、打包产物或运行时一同分发，各自保留其原始许可；本文件列出组件、版本与本仓库使用到的许可条款，完整许可文本以各组件发行包内的 `LICENSE` 文件为准。

> 说明：本文件是发布合规清单，不改变任何第三方组件的许可条件。若某组件同时提供多种许可（双许可），本仓库按其开源许可使用。

## 1. 随包分发的字体（`src/backend_v2/resources/fonts/`）

| 文件 | 上游项目 | 许可 |
| --- | --- | --- |
| `思源黑体SourceHanSansK-Bold.TTF` | Source Han Sans (Adobe) | SIL Open Font License 1.1 |
| `NotoSans-Medium.ttf` | Noto Sans (Google) | SIL Open Font License 1.1 |
| `ChisugaShotai_Regular0.2.12.1.ttf` | ちすが書体（Klee One / 霞鹜文楷 派生） | SIL Open Font License 1.1（含保留字体名 RFN） |

OFL-1.1 允许随软件一同再分发，但要求：

1. 不得单独售卖字体本身；
2. 保留版权与许可声明（本文件即为该声明入口，随附文件名与来源）；
3. 对字体做了修改时，不得继续使用原保留字体名（Reserved Font Name）。本仓库**未修改**这三款字体文件。

`ChisugaShotai` 为 Klee One 与霞鹜文楷的派生字体，其上游同样为 OFL-1.1 且带 RFN 约束；如需替换该字体，请自行确认替换来源的许可。

**已移除的字体**：2026-10-19 起，本仓库不再分发此前随包的 25 个专有字体文件（Windows 自带的 微软雅黑/宋体/黑体/仿宋、Arial Unicode MS、MS Gothic、Windows 扩展字体，华文（ST*）、汉仪、华康、方正，以及来源不明的 `ALGER.TTF` 与无扩展名残留文件）。这些字体无自由再分发许可，如需在部署机上使用，请由使用者按其自身许可自行安装。

字体查找顺序见 `src/backend_v2/content/rendering.py` 与 `src/backend_v2/storage/font_files.py`：默认字体为思源黑体，缺失时回退到系统字体（`msyh`/`msgothic`/`Arial Unicode MS` 等由**使用者本机**提供）。

## 2. Python 运行时依赖（`requirements-cpu.txt` / `requirements-gpu.txt`）

两份清单内容相同（打包变体共用同一依赖集）。

| 组件 | 版本 | 许可 |
| --- | --- | --- |
| Flask | 3.1.3 | BSD-3-Clause |
| waitress | 3.0.2 | ZPL-2.1 |
| argon2-cffi | 25.1.0 | MIT |
| SQLAlchemy | 2.0.51 | MIT |
| psutil | 7.2.2 | BSD-3-Clause |
| PyYAML | 6.0.3 | MIT |
| pillow | 12.2.0 | MIT-CMU |
| numpy | 2.4.6 | BSD-3-Clause（含 0BSD / MIT / Zlib / CC0-1.0 组件） |
| opencv-python | 4.13.0.92 | Apache-2.0 |
| colorama | 0.4.6 | BSD-3-Clause |
| loguru | 0.7.3 | MIT |
| pypinyin | 0.55.0 | MIT |
| fonttools | 4.63.0 | MIT |
| freetype-py | 2.5.1 | MIT（依赖上游 FreeType，FTL/GPL-2.0 双许可） |
| img2pdf | 0.6.3 | LGPL-3.0 |
| reportlab | 5.0.1 | BSD-3-Clause |
| mobi | 0.4.1 | GPL-3.0-only |
| PyMuPDF | 1.28.2 | AGPL-3.0 或 Artifex 商业许可（本仓库按 AGPL-3.0 使用） |
| openai | 3.33.0 | Apache-2.0 |
| httpx | 0.28.1 | BSD-3-Clause |
| chromadb | 1.5.9 | Apache-2.0 |
| gallery-dl | 1.31.4 | GPL-2.0 |
| packaging | 26.2 | Apache-2.0 或 BSD-2-Clause |

许可注意事项：

- **PyMuPDF（AGPL-3.0）**：AGPL 要求通过网络提供修改后的程序时向用户提供对应源码。本仓库本身即 GPL-3.0 开源，符合该要求；若你在此基础上二次开发并以网络服务形式对外提供，须同样以 AGPL-3.0 提供你的修改。
- **mobi（GPL-3.0-only）与 gallery-dl（GPL-2.0）**：与本仓库的 GPL-3.0 兼容（GPL-2.0-only 组件按 GPL-3.0 第 3 条兼容性声明处理时请注意其"only"字样；本仓库未修改这两个组件）。
- **img2pdf（LGPL-3.0）**：动态使用，未修改其源码。

## 3. 前端依赖（`vue-frontend/package.json`）

运行时依赖：

| 组件 | 许可 |
| --- | --- |
| vue / vue-router / pinia | MIT |
| axios | MIT |
| marked | MIT |
| @lucide/vue | ISC |

构建期依赖：`vite`、`vitest`、`vue-tsc`、`eslint`、`eslint-plugin-vue`、`typescript-eslint`、`stylelint`、`stylelint-config-standard`、`prettier`、`jsdom`、`@vue/test-utils`、`@vitejs/plugin-vue`、`openapi-typescript`、`postcss-custom-media`、`postcss-html`、`fast-check`、`globals`、`@types/node`、`@vue/tsconfig`、`@eslint/js` 均为 MIT；`typescript`、`@playwright/test` 为 Apache-2.0；`@csstools/postcss-global-data` 为 MIT-0。

这些包只在开发与构建阶段使用，不进入打包产物（构建产物为 `src/backend_v2/static/vue/` 下自有的压缩资源）。

## 4. 打包工具

| 工具 | 版本 | 许可 |
| --- | --- | --- |
| PyInstaller | 6.19.0 | GPL-2.0-or-later，附「允许使用 PyInstaller 打包非自由程序」的例外条款 |

该例外条款意味着：使用 PyInstaller 打包出的可执行文件**不**因此被要求以 GPL 发布，本仓库的 GPL-3.0 许可仅来自项目自身选择。

## 5. 打包产物中的其他内容

- `src/backend_v2/static/vue/` 为仓库自身前端源码经 Vite 构建的体积压缩产物（MIT 许可的本仓库代码）。
- 内置提示词模板、词典与实体分类表均为本仓库自有内容。

## 6. 未纳入分发的素材

- 演示截图（`pic/*.png`，含 README 中引用的赞赏码图片）为仓库文档素材；赞赏码为项目作者个人收款二维码，使用或再分发本仓库时请按需删除或替换，避免误用他人收款信息。
- `docs/legacy-cleanup-report.md` 等开发期文档不随发布产物分发。

---

如发现遗漏的第三方组件，请在本仓库提 issue 指出；本文件会随依赖变更同步更新。
<div align="center">
<img src="pic/miyako-logo.png" alt="MiyakoKB Logo" width="128">

<h1 align="center">MiyakoKB · 漫画知识库</h1>

**把一部漫画，变成可检索、可问答、可蒸馏角色的本地知识库。**

[![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/JLBC05/MiyakoKB?include_prereleases)](https://github.com/JLBC05/MiyakoKB/releases)
[![Stars](https://img.shields.io/github/stars/JLBC05/MiyakoKB?style=social)](https://github.com/JLBC05/MiyakoKB/stargazers)

[这是什么](#-这是什么) · [界面](#-界面) · [主要功能](#-主要功能) · [运行架构](#-运行架构) · [快速上手](#-快速上手) · [源码开发](#-源码开发) · [常见问题](#-常见问题)

</div>

---

## 📖 这是什么

MiyakoKB 处理的对象是**漫画的图片页**，产出的是**结构化的知识**：

```
导入（Venera 库 / 本地目录）
  → 逐页多模态分析（概要、关键事件、连续性、实体）
  → 跨章节实体对齐（别名合并与消歧）
  → 分层聚合 → 概览 / 压缩上下文 / 剧情时间线 / 知识图谱 / 向量索引
  → 检索问答（带页码引用）· 角色 Skill 蒸馏（含人格层）
```

- **本地优先**：默认单用户本地模式，数据库、页面图片、日志都留在你自己的机器上，不必把漫画上传给第三方。
- **模型自带**：视觉（VLM）、语言（LLM）、嵌入、重排全部按 OpenAI 兼容协议接入，Base URL 与 API Key 由你在设置里填写——**仓库不内置任何默认凭据、内网地址或模型文件**。
- **也能公网部署**：内置多用户能力（邀请码注册、资产配额、公网功能策略、任务调度），适合自建在线体验站。

## 🖼️ 界面

<p align="center">
  <img src="pic/bookshelf-display.png" width="85%" alt="书架">
  <br><em>书架</em>
</p>

<p align="center">
  <img src="pic/book-details.png" width="85%" alt="书籍详情">
  <br><em>书籍详情与章节</em>
</p>

<p align="center">
  <img src="pic/analysis-overview.png" width="85%" alt="分析总览">
  <br><em>知识库分析总览</em>
</p>

<p align="center">
  <img src="pic/analysis-display.png" width="85%" alt="分析结果">
  <br><em>分析结果与实体</em>
</p>

<p align="center">
  <img src="pic/timeline.png" width="85%" alt="剧情时间线">
  <br><em>剧情时间线</em>
</p>

## ✨ 主要功能

| 模块 | 说明 |
| --- | --- |
| **漫画导入** | 从 [Venera](https://github.com/wgh136/venera) 本地库导入，或从本地目录导入（先预览再确认）；按章落库为页与页面资产 |
| **逐页结构化分析** | 视觉模型逐页抽取页面概要、关键事件、连续性备注，以及角色 / 地点 / 物品 / 能力 / 概念五类实体 |
| **实体对齐** | 跨章节消歧与别名合并：硬性规则（泛词与从属过滤、简称包含、同音与异体字、日期与编号区分）+ 整图批量语义裁决；裁决缓存按书隔离 |
| **分层聚合** | 页 → 分组 → 章 → 全书的多层汇总，可按范围触发（全书 / 增量 / 指定章节 / 指定页） |
| **概述与压缩上下文** | 无剧透概览、剧情总结、压缩上下文，作为问答的全库资料 |
| **剧情时间线** | 由分析结果生成事件与角色时间线 |
| **知识图谱** | 实体共现关系图，支持类别过滤与最小权重调节 |
| **智能问答** | 单一「通用」检索：页面向量 + 全库产物 + 实体知识网络自动融合，回答带页码引用可溯源 |
| **向量检索** | ChromaDB 持久化索引 + 嵌入模型，可随时重建；索引过期会提示重建 |
| **角色 Skill 蒸馏** | 从实体知识网络与页面证据蒸馏单个角色的档案：事实层（身份、关系、出场）＋人格层（性格、说话方式、逐字台词），人格层由视觉模型查看原画提炼；可在线查看或导出 `.zip` |
| **书架与阅读器** | 书架、书籍详情、章节列表、阅读器（翻页 / 连续滚动 / 沉浸模式）、问答笔记 |
| **设置中心** | VLM / LLM / 嵌入 / 重排 / 提示词 / 批量参数，含连接测试与自定义模型档案 |
| **管理后台** | 用户与状态、邀请码、恢复码、资产配额、公网功能与模型策略、任务调度参数 |
| **任务中心** | 持久任务队列：进度、暂停 / 继续 / 取消、失败项补跑（`retry-failed`） |

## 🏗️ 运行架构

<p align="center">
  <img src="docs/assets/architecture.png" width="100%" alt="MiyakoKB 运行架构：浏览器 → Web 前端 → API → Worker，以及 SQLite / 对象存储 / 向量索引三层数据">
  <br><em>矢量版：<a href="docs/assets/architecture.svg">architecture.svg</a></em>
</p>

```text
受管启动器（saber_v2.py --role launcher）
├── 存储准备：版本检查 / 等待升级 / 健康确认（storage-migrator）
├── API 进程（Flask + Waitress）──── Web 前端（Vue 3 构建产物，同源托管）
└── Worker 进程 ──────────────────── 持久任务队列（分析 / 衍生构建 / 向量 / 导入）
```

- **存储契约**：SQLite 结构由 `src/storage_migrator/schemas/<版本>.sql` 冻结契约 + 迁移脚本管理，启动时自动校验；结构不匹配会拒绝启动，而不是带着脏结构运行。当前存储版本见 [`version.json`](version.json)。
- **任务模型**：所有重活都是持久任务（job / item / step），支持中断续跑与失败项补跑；进程崩溃后由启动器重启并回收在途工作。
- **前端**：构建产物随包分发（`src/backend_v2/static/vue`），由 API 直接托管；开发时用 Vite 热更新。
- **模型**：分析用的模型配置在任务启动时冻结，运行中改设置只影响下一个任务。

## 🚀 快速上手

### 1. 获取程序

- **发行包**：到 [Releases](https://github.com/JLBC05/MiyakoKB/releases) 下载 `MiyakoKB-<版本>-*.7z.001` 及其后续分卷，全部放在同一目录后用 [7-Zip](https://www.7-zip.org/) 或 [Bandizip](https://www.bandisoft.com/bandizip/) 解压（Windows 自带解压与部分工具无法正确识别分卷 7z）。
- **源码运行**：见下方[源码开发](#-源码开发)。
- 发行包不含本地模型运行时：视觉 / 语言 / 嵌入 / 重排全部通过 API 接入，首次启动后在「设置」中填写。

### 2. 配置模型与凭据

启动后进入「设置」：

1. **凭据**：填写各家服务的 API Key（落库后仅以密文保存，界面只显示指纹）。
2. **视觉模型（VLM）**：逐页分析用，必须支持图片输入。
3. **语言模型（LLM）**：实体对齐的语义裁决、概览等衍生步骤用，可与 VLM 复用同一提供商。
4. **嵌入 / 重排**：问答检索用；填好后点「连接测试」验证。

> 提示：Base URL 填到兼容层（通常以 `/v1` 结尾）即可。多数服务对图片体积敏感，建议把「图片最长边」压到 1024 再送模型，既稳又快。

### 3. 导入漫画并构建知识库

1. **导入**：书架页 → 导入 → 选择 Venera 本地库或本地目录（先预览，再确认）。
2. **分析**：书籍详情页 → 选择范围（全书 / 指定章节 / 指定页）→ 启动分析，进度在任务中心查看。
3. **看结果**：分析完成后查看概览、时间线、知识图谱、实体浏览与角色 Skill。
4. **提问**：问答页选书提问，回答会给出「第 X 话 第 Y 页」的引用，可点开原图核对。

### 4. 数据保存在哪里

数据库、页面资产、日志、向量索引都在数据根目录下（默认是程序目录旁的 `data-v2/`，可用 `--data-dir` 指定；公网 profile 使用独立目录）。升级前请自行备份该目录。

## 🛠️ 源码开发

```bash
# 后端
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements-cpu.txt

# 前端（Node 22+）
cd vue-frontend
npm ci
npm run build                     # 产物写入 ../src/backend_v2/static/vue
```

启动：

```bash
# 开发栈：API + Worker（不做存储准备）
python run.py --no-browser

# 正式栈：受管启动器（存储准备 + API + Worker 监督，推荐）
python saber_v2.py --role launcher

# 也可单独起某个角色
python saber_v2.py --role api
python saber_v2.py --role worker
python saber_v2.py --role storage-migrator --data-dir <目录>
```

测试与门禁：

```bash
python -m pytest tests_backend -q          # 后端全量
cd vue-frontend
npm run test                               # vitest（含 property 用例）
npm run typecheck                          # vue-tsc
npm run lint && npm run lint:css           # eslint / stylelint
npm run lint:ui:audit                      # UI 架构审计（token、裸控件、层级、媒体查询）
```

> 跑后端全量前先停掉开发栈（它占用 5000 端口，会让启动器与端口类用例超时），也别设 `PYTHONIOENCODING=utf-8`（会被角色子进程继承，导致按 locale 解码的探针用例假失败）。

打包：

```bash
python -m PyInstaller app.spec --clean     # 产物在 dist/MiyakoKB/
python .github/scripts/probe_package.py dist/MiyakoKB/MiyakoKB.exe   # 逐角色启动探测
```

发布：推送 `v*` tag 或手动触发 `.github/workflows/release.yml`；工作流会跑后端测试 + 前端门禁 + 打包探测，然后上传分卷发行资产。`version.json` 的 `version` 必须与 tag 一致。

## ❓ 常见问题

- **全新目录启动后 API 起不来？** 先看同目录的 `saber-api-diagnostic.log`；启动器只在受管模式下做存储准备，`run.py` 不做，所以开发栈不适合验证全新安装。
- **分析或衍生构建报 502 / 超时？** 多半是模型服务侧的限流或网关故障。失败项不会丢：到任务中心用「重跑失败项」补跑即可。
- **问答按钮是灰的？** 页面提示会给出缺失项（没有分析结果 / 向量索引过期 / 全库产物过期），点提示里的按钮重建即可。
- **想换模型提供商？** 任务的模型配置在启动时冻结，改设置只影响下一个任务；换完请重新触发分析（已发布的页结果可用「增量」范围复用）。
- **实体对齐很慢？** 它是把整张候选图分批送模型裁决，规模取决于书的大小与概念类实体数量；`INSIGHT_ALIGN_*` 环境变量可调批大小、并发与概念最小页数。
- **存储版本升级？** 见 [`docs/storage-migrations.md`](docs/storage-migrations.md)。

## ❤️ 贡献与反馈

- 问题反馈 / 功能建议：[Issues](https://github.com/JLBC05/MiyakoKB/issues)
- 提交前请确保后端 `pytest` 与前端 `test / typecheck / lint / lint:css / lint:ui:audit` 全绿，且重建后的前端产物已一并提交。
- 第三方组件与字体许可清单见 [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md)；本版本变更见 [`RELEASE_NOTES.md`](RELEASE_NOTES.md)。

## ⚠️ 免责声明

本项目仅用于学习与技术交流。请在遵守当地法律法规与版权政策的前提下使用，严禁用于非法用途；漫画内容版权归原作者与出版方所有，请勿传播未授权内容。

## 📄 许可证

[GPL-3.0](LICENSE)

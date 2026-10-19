<div align="center">
<img src="pic/logo.png" alt="MiyakoKB Logo" width="200">

<h1 align="center">MiyakoKB · 漫画知识库</h1>

**把漫画变成可检索、可问答的本地知识库。**

[![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/JLBC05/MiyakoKB?include_prereleases)](https://github.com/JLBC05/MiyakoKB/releases)
[![Stars](https://img.shields.io/github/stars/JLBC05/MiyakoKB?style=social)](https://github.com/JLBC05/MiyakoKB/stargazers)

[功能](#-主要功能) · [快速上手](#-快速上手) · [运行架构](#-运行架构) · [源码开发](#-源码开发) · [常见问题](#-常见问题)

</div>

---

## 📖 这是什么

MiyakoKB 把漫画（图片页）转成结构化知识库：**导入 → 逐页分析 → 跨章节实体对齐 → 图谱 / 时间线 / 概述 → 检索问答**。

- **本地优先**：默认单用户本地模式，数据（数据库、图片、日志）都留在你自己的机器上，不需要把漫画上传到任何第三方服务。
- **自带模型接入**：VLM（视觉分析）、LLM（语义裁决）、嵌入与重排模型都按 OpenAI 兼容协议接入，Base URL 与 API Key 由你在设置里填写，**项目不内置任何默认凭据或内网地址**。
- **也可公网部署**：内置多用户能力（邀请码注册、资产配额、公网策略、任务调度），适合自建在线体验站。

## ✨ 主要功能

| 模块 | 说明 |
| --- | --- |
| **漫画导入** | 从 [Venera](https://github.com/wgh136/venera) 本地库导入、从本地目录导入（先预览再确认），逐章落库为页 + 页面资产 |
| **逐页结构化分析** | 用视觉模型对每页抽取页面概要、关键事件、连续性备注与实体（角色 / 地点 / 物品 / 能力 / 概念） |
| **实体对齐** | 跨章节消歧与别名合并：硬性前置规则（泛词剥离、指代与从属过滤、拼音/异体字同形判定、日期与编号区分）+ LLM 语义裁决；裁决结果按书隔离缓存 |
| **多层级分析** | 页 → 章 → 分组 → 全书的多层汇总，可按范围（全书 / 增量 / 指定章节 / 指定页）触发 |
| **概述与压缩上下文** | 无剧透概览、剧情总结、压缩上下文，作为通用问答的全库资料 |
| **剧情时间线** | 按分析结果生成事件与角色时间线 |
| **知识图谱** | 实体关系图，支持类别过滤与最小权重调节 |
| **智能问答** | 通用问答（页面级向量检索 + 全库产物），回答带页码引用可溯源；支持父块上下文、推理检索、重排开关 |
| **向量检索** | ChromaDB 持久化索引 + 嵌入模型，可随时重建；索引过期会提示重建 |
| **书架与阅读器** | 书架、书籍详情、章节列表、阅读器（翻页 / 连续滚动 / 沉浸模式）、问答笔记 |
| **设置中心** | VLM / LLM / 嵌入 / 重排 / 提示词 / 批量参数，含连接测试与自定义模型档案 |
| **管理后台** | 用户与状态、邀请码、恢复码、资产配额、公网功能与模型策略、任务调度参数 |
| **任务中心** | 持久任务队列：进度、暂停 / 继续 / 取消、失败项补跑（`retry-failed`） |

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

## 🚀 快速上手

### 1. 获取程序

- **发行包**：到 [Releases](https://github.com/JLBC05/MiyakoKB/releases) 下载 `MiyakoKB-<版本>-*.7z.001` 及其后续分卷，放在同一目录后用 [7-Zip](https://www.7-zip.org/) 或 [Bandizip](https://www.bandisoft.com/bandizip/) 解压（Windows 自带解压与部分工具无法正确识别分卷 7z）。
- **源码运行**：见下方[源码开发](#-源码开发)。
- 发行包已不含本地模型运行时：视觉 / 语言 / 嵌入 / 重排模型全部通过 API 接入，首次启动后在「设置」中填写。

### 2. 配置模型与凭据

启动后进入「设置」：

1. **凭据**：填写各家服务的 API Key（保存后仅以密文形式落库，界面只显示指纹）。
2. **视觉模型（VLM）**：用于逐页分析，需要支持图片输入。
3. **语言模型（LLM）**：用于实体对齐的语义裁决，可与 VLM 复用同一提供商。
4. **嵌入 / 重排**：问答检索用；填好后可点「连接测试」验证。

> 提示：Base URL 填到 `/v1` 一层即可；许多 OpenAI 兼容服务对图片体积敏感，建议在批量设置里把图片最长边压到 1024 再送模型。

### 3. 导入漫画并构建知识库

1. **导入**：书架页 → 导入 → 选择 Venera 本地库或本地目录（先预览，再确认导入）。
2. **分析**：书籍详情页 → 选择范围（全书 / 指定章节 / 指定页）→ 启动分析，进度在任务中心查看。
3. **看结果**：分析完成后可查看概览、时间线、知识图谱、实体浏览器。
4. **提问**：进入问答页选择书籍提问，回答会给出引用页码，可点开原图核对。

### 4. 数据保存在哪里

数据库、页面资产、日志都放在数据根目录下（默认是程序目录旁的 `data-v2/`，可用 `--data-dir` 指定；公网 profile 使用独立目录）。升级前请自行备份该目录。

## 🏗️ 运行架构

```
受管启动器（saber_v2.py --role launcher）
├── 存储准备：版本检查 / 等待升级 / 健康确认（storage-migrator）
├── API 进程（Flask + Waitress）── Web 前端（Vue 3 构建产物）
└── Worker 进程 ── 持久任务队列（分析 / 衍生构建 / 向量 / 导入）
```

- **存储契约**：SQLite 结构由 `src/storage_migrator/schemas/<版本>.sql` 冻结契约 + 迁移脚本管理，启动时自动校验；结构不匹配会拒绝启动而不是带着脏结构运行。当前存储版本见 [`version.json`](version.json)。
- **任务模型**：所有重活都是持久任务（job/item/step），支持中断续跑与失败项补跑；进程崩溃后由启动器重启并回收。
- **前端**：构建产物随包分发（`src/backend_v2/static/vue`），由 API 直接托管；开发时用 Vite 热更新。

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
npm run test                               # vitest
npm run typecheck                          # vue-tsc
npm run lint && npm run lint:css           # eslint / stylelint
npm run lint:ui:audit                      # UI 架构审计
```

打包：

```bash
python -m PyInstaller app.spec --clean     # 产物在 dist/MiyakoKB/
python .github/scripts/probe_package.py dist/MiyakoKB/MiyakoKB.exe   # 逐角色启动探测
```

发布：推送 `v*` tag 或手动触发 `.github/workflows/release.yml`；工作流会跑后端测试 + 前端门禁 + 打包探测，然后上传分卷发行资产。

## ❓ 常见问题

- **全新目录启动后 API 起不来？** 先看 `saber-api-diagnostic.log`（同目录），启动器只在受管模式下做存储准备；`run.py` 不做，因此开发栈不适合验证全新安装。
- **分析/衍生构建报 502 或超时？** 模型服务侧的限流或网关故障。失败项不会丢：在任务中心用「重跑失败项」补跑即可。
- **问答按钮是灰的？** 页面提示会给出缺失项（没有分析结果 / 向量索引过期 / 全库产物过期），点提示里的按钮即可重建。
- **想换模型提供商？** 任务的模型配置在启动时冻结，改设置只影响下一个任务；切换后请重新触发分析。
- **存储版本升级？** 见 [`docs/storage-migrations.md`](docs/storage-migrations.md)。

## ❤️ 贡献与反馈

- 问题反馈 / 功能建议：[Issues](https://github.com/JLBC05/MiyakoKB/issues)
- 提交前请确保后端 `pytest`、前端 `test / typecheck / lint / lint:css / lint:ui:audit` 全绿。
- 第三方组件与字体许可清单见 [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md)；本版本变更见 [`RELEASE_NOTES.md`](RELEASE_NOTES.md)。

## ⚠️ 免责声明

本项目仅用于学习与技术交流。请在遵守当地法律法规与版权政策的前提下使用，严禁用于非法用途；漫画内容版权归原作者与出版方所有，请勿传播未授权内容。

## 📄 许可证

[GPL-3.0](LICENSE)
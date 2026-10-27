#!/usr/bin/env node
/**
 * 从 RELEASE_NOTES.md 抽出指定版本的更新日志，供 release workflow 填进 release body。
 *
 * 用法：
 *   node .github/scripts/release_notes.cjs 0.3.4
 *   node .github/scripts/release_notes.cjs 0.3.4 --with-downloads
 *
 * 约定：RELEASE_NOTES.md 里每个版本是二级标题 `## vX.Y.Z`，正文到下一个 `## ` 为止。
 * 找不到该版本时退出码 2 —— 发布流程宁可失败也不要发出空更新日志。
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");

/**
 * 抽出某个版本的正文。
 *
 * @param {string} markdown RELEASE_NOTES.md 全文
 * @param {string} version 版本号，可带或不带 `v` 前缀
 * @returns {string | null} 正文（已去掉首尾空行），找不到返回 null
 */
function extractSection(markdown, version) {
  const wanted = String(version).replace(/^v/, "").trim();
  const lines = markdown.split(/\r?\n/);
  const heading = new RegExp(`^##\\s+v?${wanted.replace(/\./g, "\\.")}\\s*$`);
  let start = -1;
  for (let index = 0; index < lines.length; index += 1) {
    if (heading.test(lines[index].trim())) {
      start = index + 1;
      break;
    }
  }
  if (start === -1) {
    return null;
  }
  const body = [];
  for (let index = start; index < lines.length; index += 1) {
    if (/^##\s+/.test(lines[index])) {
      break;
    }
    body.push(lines[index]);
  }
  const text = body.join("\n").trim();
  return text || null;
}

/**
 * 下载说明。版本号只出现在文件名里，其余与 workflow 中的模板保持一致。
 *
 * @param {string} version 版本号
 * @returns {string}
 */
function downloadSection(version) {
  return [
    "---",
    "",
    "### 下载说明",
    "",
    "📦 **普通用户：**",
    `- 下载 \`MiyakoKB-${version}.7z\``,
    "- 使用 **7-Zip** 或 **Bandzip** 解压后运行 `MiyakoKB.exe`",
    "",
    "**首次启动**：按向导选择数据目录，然后在「设置」里配置 VLM / LLM / 嵌入 / 重排四条通道的 API Key，再导入漫画。",
    "",
    "🔧 **源码部署用户：**",
    "- 按 README 的「源码开发」一节准备 Python 3.11+、Node 22+ 环境，`pip install -r requirements.txt`，`npm ci && npm run build`，然后用 `python saber_v2.py --role launcher` 启动。",
  ].join("\n");
}

/**
 * 拼出完整的 release body。
 *
 * @param {string} markdown RELEASE_NOTES.md 全文
 * @param {string} version 版本号
 * @returns {string | null}
 */
function buildBody(markdown, version) {
  const notes = extractSection(markdown, version);
  if (!notes) {
    return null;
  }
  const clean = String(version).replace(/^v/, "").trim();
  return [
    `## MiyakoKB v${clean}`,
    "",
    "### 更新日志",
    "",
    notes,
    "",
    downloadSection(clean),
    "",
  ].join("\n");
}

function main(argv) {
  const withDownloads = argv.includes("--with-downloads");
  const version = argv.find((item) => !item.startsWith("-"));
  if (!version) {
    process.stderr.write("usage: node .github/scripts/release_notes.cjs <version> [--with-downloads]\n");
    return 1;
  }
  const root = path.resolve(__dirname, "..", "..");
  const file = path.join(root, "RELEASE_NOTES.md");
  let markdown;
  try {
    markdown = fs.readFileSync(file, "utf8");
  } catch (error) {
    process.stderr.write(`cannot read ${file}: ${error.message}\n`);
    return 1;
  }
  const body = withDownloads ? buildBody(markdown, version) : extractSection(markdown, version);
  if (!body) {
    process.stderr.write(`RELEASE_NOTES.md has no section for v${version}\n`);
    return 2;
  }
  process.stdout.write(body.endsWith("\n") ? body : `${body}\n`);
  return 0;
}

if (require.main === module) {
  process.exitCode = main(process.argv.slice(2));
}

module.exports = { buildBody, downloadSection, extractSection };

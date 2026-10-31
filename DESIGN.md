# MiyakoKB 设计规范（DESIGN.md）

> 本文件是 de-slop 改造的**唯一取值来源**。界面里所有颜色、字号、圆角、间距、按钮、图标、动效都必须引用这里，不再各页各写。
> 原则：**黑白灰为底，颜色只给内容和最重要的一个按钮；一层平面，不加阴影堆叠；图标全用 Lucide；动效只做反馈。**

## 1. 颜色

### 1.1 中性色（主色板，承担 95% 面积）

| Token | Light | Dark | 用途 |
|---|---|---|---|
| `--color-bg-base` | `#F7F7F8` | `#161618` | 页面底色（中性灰，**去掉原来的紫调**） |
| `--color-surface` | `#FFFFFF` | `#1F1F22` | 卡片 / 面板底 |
| `--color-surface-secondary` | `#F2F2F3` | `#26262A` | 次级填充（导航条、分组底） |
| `--color-text-primary` | `#1F1F22` | `#F2F2F3` | 主文字 |
| `--color-text-secondary` | `#6B6B72` | `#A8A8B0` | 次要文字 |
| `--color-text-muted` | `#9A9AA2` | `#75757D` | 弱化说明 |
| `--color-border` | `#E4E4E7` | `#333338` | 分割线 / 卡片描边 |

### 1.2 强调色（全站唯一彩色，只给"最重要的操作 + 选中态 + 可点击高亮"）

| Token | Value | 用途 |
|---|---|---|
| `--color-brand-primary` | `#9977CC` | 主按钮底、选中态、链接 |
| `--color-brand-hover` | `#7E5CB3` | 主按钮 hover |
| `--color-brand-soft` | `#EFE8F8` | 选中浅底（替代原来的紫灰填充） |
| `--color-brand-text` | `#6B4FA0` | 彩色文字需要时（深一档，保证对比度） |

### 1.3 语义状态色（**去荧光、低饱和**，只用于状态文字/小圆点/描边，不做整块实心主按钮）

| Token | Light | 用途 |
|---|---|---|
| `--color-success` | `#2E7D32` | 成功（已构建）文字/点 |
| `--color-warning` | `#B26A00` | 进行中/警示 |
| `--color-danger` | `#C0392B` | 错误/删除 |
| `--color-info` | `#4A5568` | 信息（用中性深灰，不再用蓝） |

> **明确删除**：`#00ff88` 荧光绿、`#3498db` / `#667eea` / `#764ba2` / `#6366f1` / `#4f46e5` 一整套蓝紫渐变近似色、`#17a2b8` 青色，以及图谱里写死的 6 个彩虹色 `#8b5cf6/#a855f7/#eab308/#06b6d4/#10b981/#f97316`（统一由强调色 + 中性灰 + 位置区分类别）。

## 2. 字号（全站只 4 档，最小 12px）

| Token | 值 | 用途 |
|---|---|---|
| `--font-size-xs` | `12px` | 注释、辅助、徽标 |
| `--font-size-sm` | `14px` | 正文默认 |
| `--font-size-md` | `16px` | 区块标题、按钮大字 |
| `--font-size-lg` | `20px` | 页面标题 |

层级靠**字重（400/500/600/700）+ 颜色 + 间距**拉开，**禁止**再用 0.65/0.72/0.78/0.84/0.88/0.92/0.94rem 这类微差字号。**所有 10/11/11.5px 一律升到 12px 起。** 行高：正文 1.5，标题 1.3。

## 3. 圆角（两档 + 药丸）

| Token | 值 | 用途 |
|---|---|---|
| `--radius-control` | `8px` | 按钮、输入框、下拉、标签块 |
| `--radius-container` | `12px` | 卡片、弹窗、面板 |
| `--radius-pill` | `999px` | 胶囊/药丸（仅此用） |

嵌套规则：**内圆角 = 外圆角 − 内边距**（例：卡片 12 + padding 4 → 内层控件 8）。

## 4. 间距 / 阴影 / 描边

- 间距档：4 / 8 / 12 / 16 / 24 / 32（`--space-1..6`）。
- **卡片默认不用阴影**，用 `1px solid var(--color-border)` 描边；仅浮层（弹窗/抽屉/下拉）可用一层轻阴影 `0 8px 24px rgba(0,0,0,0.12)`。取消原来的 `0 4px 12px` 常驻卡阴影与 hover 大阴影。
- 分割：空白 > 底色 > 1px 分割线；卡片左侧无状态时不加彩色竖条。

## 5. 按钮规范

| 变体 | 外观 |
|---|---|
| `primary` | 纯色 `--color-brand-primary`，白字，圆角 8，hover `--color-brand-hover`，**无渐变、无发光** |
| `secondary` | 白底 + 1px `--color-border`，主文字色，hover 浅灰底 |
| `ghost` | 透明底，主文字色，hover 浅灰底 |
| `danger` | 透明/白底 + 红字 + 红描边；只有"最终确认删除"才可实心红 |
| `success` | **不再是荧光实心绿**：走 secondary/ghost + 绿色对勾图标表示"已完成"，或仅文字绿 |

一屏**最多一个 primary**。同排两个动作时，次要的降为 secondary/ghost。

## 6. 图标

- 统一用 **Lucide**（项目已装 `@lucide/vue`，经 `src/components/ui/UiIcon.vue` + `src/components/ui/iconRegistry.ts`）。
- 尺寸：行内 16px、导航/工具 18px、空状态 32–40px；颜色继承文字色。
- **禁止**用 emoji 当图标、禁止手画 SVG。

## 7. 动效

- 只做反馈：hover/active 的**颜色、透明度**过渡，时长 ≤ 150ms ease。
- **禁止**：`translateY/scale` 位移动画、无限循环脉冲（`pulse-signal`）、进度条光扫（`progress-glare`）、发光 `box-shadow: 0 0 Npx` 装饰。加载态保留 `spin`、打字态保留 `dots`、浮层进出保留 fade。

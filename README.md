# 小红书互动工具集（养号版）

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![Python](https://img.shields.io/badge/python-3.10-blue)](https://www.python.org/) [![Node](https://img.shields.io/badge/node-18%2B-brightgreen)](https://nodejs.org/)

小红书互动工具集（养号版）：通过 [BrowserMCP](https://github.com/Smotree/BrowserMCP) 控制用户已登录的真实 Chrome，结果导出 Excel。

> 不是批量挂机工具，而是让每一次互动，都更像真人。
## 功能特性

- **拟人化互动**：随机滚动/阅读/输入节奏、逐字评论、长休息，避免机械行为特征。
- **真实浏览器操作**：通过 BrowserMCP 控制用户已登录的 Chrome，全程 DOM 操作，不调私有 API。
- **定向评论模式**：搜索 + 目标作者判断（目标词表/排除词表），只对匹配笔记评论，同作者跨运行去重。
- **双模式点赞**：频道刷赞 + 搜索关键词刷赞，已赞笔记自动跳过。
- **关键词搜索过滤**：时间窗口、点赞区间、作者排除、经销商识别，结果带 `xsec_token` 链接与封面下载。
- **隐私脱敏**：昵称掩码 + 用户 ID 匿名哈希，导出文件不可反查账号。
- **增量日志**：每条记录即时落盘，崩溃只丢最后一条；输出文件带时间戳，零覆盖。
- **配置驱动**：全部行为参数进 `config.json`，代码零魔法数字。

## 目录

- [功能特性](#功能特性)
- [脚本](#脚本)
- [文件结构](#文件结构)
- [架构 (Architecture)](#架构-architecture)
- [工作原理 (How It Works)](#工作原理-how-it-works)
- [环境要求](#环境要求)
- [安装](#安装)
- [使用](#使用)
- [配置概览](#配置概览)
- [开发命令](#开发命令)
- [数据与安全](#数据与安全)
- [贡献与许可证](#贡献与许可证)
- [社区](#社区)
- [作者](#作者)

## 脚本

| 脚本 | 性质 | 核心功能 | 输出 |
|------|------|----------|------|
| `collect.py` | 只读 | 多频道笔记+评论采集 | `xhs-collect-{ts}.xlsx` |
| `reply.py` | 写操作 | 拟人评论（普通/定向模式） | `xhs-reply-log-{ts}.xlsx` |
| `like.py` | 写操作 | 拟人点赞（频道/搜索） | `xhs-like-log-{ts}.xlsx` |
| `search.py` | 只读 | 关键词搜索+过滤 | `xhs-search-{ts}.xlsx` |

## 文件结构

```
xiaohongshu-hudong-tools/
├── collect.py         # 采集
├── reply.py           # 评论
├── like.py            # 点赞
├── search.py          # 搜索
├── exceptions.py      # 异常定义（共享）
├── exception_map.py   # 错误码映射（共享）
├── privacy.py         # 隐私脱敏（共享）
├── cover_utils.py     # 封面下载（共享，reply/like/search 用）
├── sort_utils.py      # 评论排序（共享，reply/search 用）
├── config.example.json
├── requirements.txt
└── LICENSE
```

> 兼容性：Windows 10/11；Python 3.10+；Node.js 18+；Chrome。

## 架构 (Architecture)

四层结构，脚本本身**不调用小红书任何私有 API**，全程操作真实浏览器 DOM：

```
┌──────────────────────────────────────────────────────────────┐
│ 第 1 层  脚本层（Python）                                      │
│   collect.py / search.py（只读）  reply.py / like.py（写操作） │
│   + 共享模块：cover_utils / sort_utils / privacy / exceptions │
└──────────────┬───────────────────────────────────────────────┘
               │ HTTP POST http://localhost:12800/exec
               │ {action, params}   （send_cmd，tenacity 重试 3 次）
┌──────────────▼───────────────────────────────────────────────┐
│ 第 2 层  BrowserMCP Relay Server（Node.js，端口 12800）        │
│   命令桥：HTTP → WebSocket → 扩展；/health 健康检查             │
└──────────────┬───────────────────────────────────────────────┘
               │ WebSocket（扩展常驻连接）
┌──────────────▼───────────────────────────────────────────────┐
│ 第 3 层  Chrome 扩展（background.js）                          │
│   navigate / execute_js / click / type / scroll / list_tabs…  │
└──────────────┬───────────────────────────────────────────────┘
               │ chrome.* API 操作真实标签页
┌──────────────▼───────────────────────────────────────────────┐
│ 第 4 层  真实 Chrome（用户已登录的小红书网页）                  │
│   feed 滚动 → 打开笔记弹层 → 评论区 → 搜索页卡片 → 点赞/评论    │
└───────────────────────────────────────────────────────────────┘
```

| 模块 | 职责 |
|---|---|
| `collect.py` | 采集入口：频道 feed 滚动 → 弹层详情 → 评论区批量提取；`send_cmd` 供 `search.py` 复用 |
| `search.py` | 搜索入口：搜索词导航 → 排序切换 → 两阶段拟人滚动 → 卡片层提取 + 过滤 |
| `reply.py` | 写操作：feed 找笔记 → 拟人逐字输入评论（普通/定向模式） |
| `like.py` | 写操作：频道/搜索两种任务队列 → 拟人点赞（已赞检测跳过） |
| `cover_utils.py` | 封面下载：配置读取、下载、超时/大小限制、Excel 超链接列 |
| `sort_utils.py` | 评论排序切换（最新/综合）的 DOM 点击封装，`reply.py`/`search.py` 共用 |
| `privacy.py` | 昵称掩码 + 用户 ID 匿名哈希 |
| `exceptions.py` / `exception_map.py` | 异常体系：`MCPDownError` 等 + 错误码→中文提示映射 |

### 与常见替代方案的对比

**一句话**：Computer Use 是“AI 看屏幕猜按钮去点”，本工具是“脚本直接操作你已登录的浏览器”，裸 CDP 是中间那条路但容易被风控。

| | ChatGPT Computer Use | 裸 CDP（Playwright 等） | 本工具 |
|---|---|---|---|
| 控制方式 | 截图 + 视觉推理 + 模拟点击 | 调试端口发指令 | Chrome 扩展 API 桥接 |
| 登录态 | 需独立环境重搞 | 新 profile，无登录态 | **直接复用已登录的 Chrome** |
| 风控特征 | 屏幕级，无浏览器痕迹 | webdriver 特征明显 | 无调试特征，像真人操作 |
| 速度 / 成本 | 慢、贵（按步计费） | 快 | 快 |
| 拟人化 + 业务逻辑 | 无 | 无 | **有**（随机节奏 / 已赞去重 / 定向评论 / Excel 导出） |

Computer Use 是通用“手”，CDP 是通用“遥控器”，本工具是给小红书养号定制的“代练”。

## 工作原理 (How It Works)

1. **控制链路**：脚本通过 `send_cmd` 向 BrowserMCP（localhost:12800）发 `{action, params}`；Server 经 WebSocket 转发给 Chrome 扩展；扩展用 `chrome.*` API 操作**用户已登录的真实 Chrome**，全程走 DOM，不碰反爬签名。
2. **数据提取**：以 `execute_js` 注入 IIFE 为主——单次往返在浏览器内完成「滚动 N 轮 + 去重提取 + 回传」（如评论区 `BATCH_EXTRACT_JS`），将 5~8 次往返压缩为 1 次；DOM 提取失败时回退 `window.__INITIAL_STATE__`。
3. **拟人化**：滚动距离/间隔、阅读时长、停留、休息均按 `browse` 配置的随机区间取值；评论逐字输入（`type_char_range`）+ 回看（`review_range`）+ 随机间隔，模拟真人节奏。
4. **写操作防御**：点赞前检测 `#liked` 已赞标记跳过；评论/点赞都要求笔记 ≥ `min_likes` 门槛；每条之间随机间隔、每 N 条长休息，降低风险。
5. **容错**：`tenacity` 对 `MCPDownError`（MCP 抖动/超时）自动重试 3 次；代码错误不重试；`exception_map` 将异常映射为可读中文提示。
6. **隐私**：`privacy.mask_nickname` 掩码昵称、`anonymize_user_id` 对用户 ID 做不可逆哈希后写入 Excel。
7. **输出**：全部结果导出 Excel（`output/`）：采集评论表、评论/点赞日志表、搜索三 sheet（成功笔记 / 过滤明细 / 失败明细）。

### 只读 / 写操作矩阵

| 脚本 | 性质 | 运行时影响 |
|---|---|---|
| `collect.py` | 只读 | 仅浏览，可安全空跑 |
| `search.py` | 只读 | 仅浏览 + 点击排序 chip，可安全空跑 |
| `reply.py` | 写操作 | 会**真实发送评论** |
| `like.py` | 写操作 | 会**真实产生点赞** |

## 环境要求

- Windows 10/11；
- Python 3.10 或更高版本；
- Node.js 18 或更高版本；
- Chrome；
- 已安装并连接的 BrowserMCP 扩展。

## 安装

### 1. 用自然语言安装整个项目（推荐）

**把下面这段话直接发送给 Claude Code、Codex、OMP 或 OpenCode**，Agent 会自动识别操作系统、执行全部安装步骤并报告结果：

> 请在这个项目的根目录完成安装，所有文件只允许放在项目目录内，禁止全局安装、禁止使用 conda：
>
> 1. **Python 环境**：用官方 python.org 的 Python 3.10+ 在项目内创建 `.venv`，pip 缓存指向 `.local/pip-cache`，然后安装 `requirements.txt`；运行和验证都用 `.venv` 里的 Python，不要动系统 Python；
> 2. **BrowserMCP**：克隆 https://github.com/Smotree/BrowserMCP.git 到项目内的 `.local/BrowserMCP`，在其目录下运行 npm 安装（npm 缓存指向项目根的 `.local/npm-cache`，注意 `../` 相对路径，不要嵌套进 `.local/BrowserMCP` 内部）并构建；
> 3. **启动服务**：在 `.local/BrowserMCP` 目录启动服务（`node server/dist/index.js`），访问 http://localhost:12800/health 确认返回 `{"status":"ok","connected":true}`；
> 4. **创建配置**：从 `config.example.json` 复制一份 `config.json`（不要修改 example 模板）；
> 5. 完成后用中文报告：`.venv` 与依赖是否就绪、BrowserMCP 安装路径与构建是否成功、服务是否连通、`config.json` 是否已创建，以及剩下的手动步骤是什么。

**剩下的 2 步必须手动完成**（浏览器相关操作无法由脚本完成）：

1. 加载扩展：`chrome://extensions` → 启用开发者模式 → “加载已解压的扩展程序” → 选 `.local/BrowserMCP/extension/` → 点击图标确认 badge 为绿色 `ON`；
2. 用 Chrome 打开 <https://www.xiaohongshu.com> 完成登录。

**自然语言日常运维**（同样直接发给 Agent）：

- **启动服务**："在 `.local/BrowserMCP` 目录启动 BrowserMCP 服务，访问 http://localhost:12800/health 确认连通，告诉我是 ok 还是失败、失败原因是什么。"
- **停止服务**："停止 BrowserMCP 服务：杀掉占用 12800 端口的所有 node 进程，确认端口已释放。"
- **重新安装 / 更新 BrowserMCP**："更新项目内的 BrowserMCP：先停止 12800 端口的 node 服务进程，删除 `.local/BrowserMCP` 目录，重新克隆 https://github.com/Smotree/BrowserMCP.git 并安装依赖、构建、启动服务、验证 health。完成后提醒我在 `chrome://extensions` 中删除旧扩展并重新加载。"

### 2. 手动安装

只有 Agent 无法自动完成时才需要手动操作。

**依赖红线**：
- 所有依赖、缓存、构建产物**只允许放在项目目录内**，禁止散落到 C 盘系统目录或全局环境；
- Python 依赖只装项目内 `.venv`（用官方 python.org Python 创建，运行用 `.venv\Scripts\python.exe`）；**禁止使用 conda**，禁止在系统 Python / 全局环境安装本项目依赖；
- Node 依赖只装项目内 `node_modules`（BrowserMCP 在 `.local/BrowserMCP/`，即 `.local/BrowserMCP/node_modules`）；**禁止全局安装**（`npm install -g`）；
- 运行时输出 `output/`、日志 `logs/`、`.venv/`、`.local/`（含 BrowserMCP）均已被 `.gitignore` 排除，**永不进 git**。

#### 2.1 Python 依赖

```powershell
# Windows PowerShell
python -m venv .venv
.venv\Scripts\activate
pip install --cache-dir .local/pip-cache -r requirements.txt
```

#### 2.2 BrowserMCP

`.local/BrowserMCP/` 是独立第三方仓库，不包含在本项目 Git 历史中：

```powershell
# Windows PowerShell（在项目根目录执行）
New-Item -ItemType Directory -Force .local | Out-Null
git clone https://github.com/Smotree/BrowserMCP.git .local/BrowserMCP
Set-Location .local/BrowserMCP
npm install --cache ../npm-cache
npm run build
node server/dist/index.js    # 启动服务（保持窗口打开）
Set-Location ../..
```

```bash
# Git Bash
mkdir -p .local
git clone https://github.com/Smotree/BrowserMCP.git .local/BrowserMCP
cd .local/BrowserMCP && npm install --cache ../npm-cache && npm run build && cd ../..
cd .local/BrowserMCP && node server/dist/index.js   # 启动服务
```

> **注意**：`--cache ../npm-cache` 的 `../` 是相对 `.local/BrowserMCP/` 的，指向项目根 `.local/npm-cache`；不要写成 `--cache .local/npm-cache`，否则会嵌套进 `.local/BrowserMCP/.local/`。
>
> 启动服务必须保持工作目录在 `.local/BrowserMCP`；停止时按 `Ctrl+C`，或杀掉占用 12800 端口的进程：
>
> ```powershell
> Get-NetTCPConnection -LocalPort 12800 | Select-Object -ExpandProperty OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force }
> ```
>
> 重新安装 / 更新：停止服务 → `Remove-Item -LiteralPath .local/BrowserMCP -Recurse -Force` → 重新执行以上命令 → 重新加载扩展。

#### 2.3 创建本地配置

```bash
# Windows cmd
copy config.example.json config.json

# Git Bash
cp config.example.json config.json
```

按实际需求编辑 `config.json`。该文件已被 `.gitignore` 排除，不应提交真实运行参数或账号相关信息。

## 使用

**两种用法，任选其一：**

- **自然语言**（推荐）：把需求直接说给 Agent（Claude Code、Codex、OMP 或 OpenCode），它会自动改配置、跑脚本、报告结果。标准话术见 **[自然语言操作手册（NL_USAGE.md）](NL_USAGE.md)**；
- **命令行**：手动运行脚本，见下方「开始前」与脚本命令。

开始前：

1. 用 Chrome 打开 <https://www.xiaohongshu.com> 并完成登录；
2. 确认 BrowserMCP 扩展为 `ON`；
3. 检查 BrowserMCP 服务是否连通（本地转发端口 12800）：

```powershell
curl http://localhost:12800/health
# 预期返回 {"status":"ok","connected":true}
```

4. 检查 `config.json`；
5. 确认当前脚本是只读还是写操作。

```bash
# 只读关键词搜索
python search.py

# 只读采集
python collect.py

# 评论：会真实发送评论
python reply.py

# 点赞：会真实产生点赞
python like.py
```

> 搜索默认按「最新」排序抓取（导航后自动点击「最新」chip，按发布日期降序），可在 `config.json` 的 `search.sort_by` 改为 `comprehensive` 恢复综合排序。
>
> **只读/写操作红线**：`collect.py`、`search.py` 只读安全；`reply.py`、`like.py` 会真实发送评论/产生点赞，运行前确认配置无误。

## 配置概览

唯一的本地配置入口是 `config.json`（从 `config.example.json` 复制）。JSON 不支持注释，字段说明与示例统一在此：

### 顶层通用字段

| 配置节 | 用途 |
|---|---|
| `channels` | 采集频道列表：`name` 为频道名（如 `推荐`/`穿搭`），`note_count` 为每个频道采集的笔记数 |
| `min_likes` | 采集/评论/点赞共用的候选笔记点赞门槛（搜索过滤使用 `search.min_likes`） |
| `max_comments` | 每条笔记最多保存的评论数（用于拼接 `commentText`） |
| `output_file` | `collect` 采集结果的输出路径（相对项目根目录） |
| `max_scroll_attempts` | feed 滚动加载的最大尝试次数，超过仍无新卡片则刷新重试 |

```json
{
  "channels": [
    { "name": "推荐", "note_count": 5 },
    { "name": "穿搭", "note_count": 5 }
  ],
  "min_likes": 100,
  "max_comments": 5,
  "output_file": "output/xhs-comments-filtered.xlsx",
  "max_scroll_attempts": 40
}
```

### `browse` — 浏览拟人参数

页面滚动/加载/阅读的拟人化节奏。所有 `*_range` 均为 `[最小值, 最大值]`，每次随机取；`http_timeout` 为 Python 调 BrowserMCP 的超时（秒），**不可删，否则 MCP 无响应时脚本永久挂起**。

```json
{
  "scroll_px_range": [300, 700],
  "scroll_pause_range": [1.5, 3.0],
  "scrolls_per_attempt": [1, 2],
  "read_time_range": [4.0, 8.0],
  "page_load_wait_range": [1.5, 2.5],
  "gap_range": [1.0, 2.0],
  "attracted_pause_prob": 0.1,
  "attracted_pause_range": [3.0, 5.0],
  "channel_switch_delay_range": [1.5, 3.0],
  "comment_scroll_step_range": [200, 500],
  "http_timeout": 60
}
```

### `collect` — 采集参数

`hot_mode=true` 时按热度排序取评论（赞权重 `hot_weight_likes`、回复权重 `hot_weight_replies`）；`get_sub_comments=true` 展开楼中楼（更慢）；`max_fail_streak` 连续失败 N 次后放弃该频道。

```json
{
  "hot_mode": true,
  "hot_comment_scroll_count": 6,
  "hot_weight_likes": 3,
  "hot_weight_replies": 1,
  "max_fail_streak": 3,
  "back_delay_range": [0.8, 2.0],
  "get_sub_comments": false,
  "expand_replies_wait_range": [1.0, 2.0],
  "sub_comment_extract_passes": 2
}
```

### `reply` — 拟人评论参数

`count` 为单次运行评论条数；`comment_texts` 为候选评论文案（每条随机取 1 条）；`interval_range` 为每条评论间随机间隔秒；`rest_every`/`rest_range` 为每 N 条长休息；`type_char_range` 等为逐字输入节奏；`download_covers` 下载目标笔记封面到 `cover_dir`。

```json
{
  "count": 3,
  "comment_texts": ["感谢分享", "写得真好，学习了"],
  "interval_range": [30, 90],
  "rest_every": 5,
  "rest_range": [120, 300],
  "think_range": [1.5, 3.0],
  "type_char_range": [0.3, 0.8],
  "type_pause_prob": 0.15,
  "type_pause_range": [0.8, 1.5],
  "review_range": [1.2, 2.5],
  "after_send_range": [2.0, 4.0],
  "download_covers": true,
  "cover_dir": "output/covers",
  "cover_timeout": 15,
  "cover_max_size_mb": 5,
  "output_file": "output/xhs-reply-log.xlsx"
}
```

#### `reply.owner_comment` — 定向评论模式

`enabled=false` 关闭。开启后只对 `keywords` 搜索到的笔记评论：`owner_markers` 命中判定为目标作者、`dealer_markers` 命中判定为排除对象（跳过，如广告/推广号）；`sort_by` 排序方式；`done_file` 记录已处理笔记。

以"健身打卡笔记"为例：

```json
{
  "enabled": false,
  "keywords": ["减脂餐", "健身打卡"],
  "count": 1,
  "comment_texts": ["请问这个食谱是减脂期能吃的吗？也想试试～"],
  "min_likes": 1,
  "sort_by": "latest",
  "done_file": "output/xhs-owner-comment-done.json",
  "owner_markers": ["打卡", "记录", "坚持"],
  "dealer_markers": ["广告", "推广", "测评号"],
  "max_tries": 20
}
```

### `like` — 拟人点赞参数

`channels` 按频道刷赞、`search_keywords` 按搜索词刷赞（两种可同时配置，`count` 为各自目标数）；`max_attempts` 单任务最大尝试次数；`refresh_every_range` 刷到 N 次后刷新 feed；`download_covers` 下载被赞笔记封面。

```json
{
  "channels": [{ "name": "推荐", "count": 10 }],
  "search_keywords": [{ "keyword": "<搜索关键词>", "count": 10 }],
  "download_covers": true,
  "cover_dir": "output/covers",
  "cover_timeout": 15,
  "cover_max_size_mb": 5,
  "max_attempts": 30,
  "read_time_range": [4.0, 8.0],
  "gap_range": [5.0, 15.0],
  "rest_every": 10,
  "rest_range": [120, 300],
  "refresh_every_range": [30, 50],
  "output_file": "output/xhs-like-log.xlsx",
  "scroll_top_attempts": 8,
  "refresh_retry_max": 10
}
```

### `privacy` — 隐私脱敏

`mask_nickname=true` 对昵称做掩码（如 `张三` → `张*`）；`anonymize_user_id=true` 对用户 ID 做匿名哈希（导出文件无法反查账号）。

```json
{
  "mask_nickname": false,
  "anonymize_user_id": false
}
```

### `search` — 关键词搜索参数

`keywords` 为搜索词列表（**注意替换为真实关键词**）；`sort_by` 排序（`latest` 最新 / `comprehensive` 综合）；`cutoff_days` 只看 N 天内的笔记；`min_likes`/`max_likes` 点赞过滤区间；`dealer_patterns` 命中判定经销商过滤；`screen_scroll` 分两阶段拟人滚动（首次 N 屏、可选刷新后二次 N 屏）。

```json
{
  "keywords": ["<搜索关键词>"],
  "sort_by": "latest",
  "cutoff_days": 90,
  "min_likes": 0,
  "max_likes": null,
  "exclude_author_substrings": ["<排除作者子串>"],
  "dealer_patterns": ["<经销商识别词>"],
  "max_notes_per_keyword": 50,
  "output_file": "output/xhs-search.xlsx",
  "scroll_pause_range": [2.5, 4.0],
  "keyword_gap_range": [25, 45],
  "http_timeout": 60,
  "screen_scroll": {
    "enabled": true,
    "first_rounds_range": [7, 15],
    "refresh_between": true,
    "second_rounds_range": [5, 8]
  }
}
```

> `config.example.json` 为公开模板：业务关键字（搜索词、经销商识别词、目标作者标记等）已替换为 `<占位词>`，复制后请替换为真实关键词；`config.json` 已被 `.gitignore` 排除，不应提交真实运行参数。

## 开发命令

```bash
.venv\Scripts\python.exe -m compileall -q collect.py reply.py like.py search.py cover_utils.py privacy.py exceptions.py exception_map.py sort_utils.py   # 语法校验
.venv\Scripts\ruff check --select E9,F63,F7,F82 *.py                                                                                                  # 关键错误 lint
python -m json.tool config.example.json > $null                                                                                                        # 配置模板校验
```

CI（`.github/workflows/quality.yml`）在 push / PR 时自动执行以上三项。

## 数据与安全

- **登录态**：只使用用户已登录的真实 Chrome，脚本不存储任何 cookie / session，无独立账号体系。
- **配置**：`config.json` 含真实运行参数，已被 `.gitignore` 排除，绝不入库；公开模板 `config.example.json` 中的业务关键字一律使用 `<占位词>`。
- **输出脱敏**：`privacy` 节可对昵称掩码、用户 ID 匿名哈希后再写 Excel。
- **依赖隔离**：Python 依赖只装项目内 `.venv`，Node 依赖只装 `.local/BrowserMCP/node_modules`，禁止全局安装与 conda；缓存可指向 `.local/pip-cache`、`.local/npm-cache`。
- **风险提示**：`reply.py` / `like.py` 为写操作，会真实发送评论 / 产生点赞，请自行评估账号风险。

## 贡献与许可证

欢迎通过 Issue 反馈问题，或提交 Pull Request 改进功能、文档和测试。提交代码前请至少运行 [开发命令](#开发命令) 中的语法校验与 lint。

本项目采用 [MIT License](LICENSE)。

## 社区

本项目在 [LINUX DO](https://linux.do/) 社区进行开源推广，感谢社区佬友的交流、反馈与建议。

## 作者

你们喜爱的老王 — [B 站 @你们喜爱的老王](https://space.bilibili.com/97727630) · [GitHub](https://github.com/ops120)

---

小红书互动工具集：让每一次互动，都更像真人。

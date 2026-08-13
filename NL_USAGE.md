# 自然语言操作手册（NL_USAGE）

面向 AI Agent：把用户的话映射到脚本操作。只覆盖本项目 4 个脚本（`collect.py` / `reply.py` / `like.py` / `search.py`）。

## 通用规则

- 临时改 `config.json` 前先备份到 `.local/`，跑完必须还原
- 写操作（评论/点赞）执行前先报计划，用户确认后才跑
- 跑完报告：成功 X / 跳过 Y / 失败 Z + 输出文件绝对路径
- 遇到 BrowserMCP 不通：`curl http://localhost:12800/health` 确认 `connected:true`

## 场景速查（标准话术）

照着说即可，`<>` 为可替换参数：

| 标准话术 | 动作 |
|---|---|
| 采集 `<频道>` `<N>` 篇笔记（每篇 `<M>` 条评论） | 改 `channels`/`max_comments` → 跑 → 还原 |
| 搜一下 `<关键词>`（最近 `<N>` 天，点赞 ≥ `<K>`） | 改 `search` 节 → 跑 → 还原 |
| 点 `<N>` 个赞（频道：`<频道>`） | 改 `like.channels` count → 跑 → 还原 |
| 评论 `<N>` 条「`<文案>`」（频道：`<频道>`） | 手工点频道 tab → 改 `reply` → 跑 → 还原 |
| 定向评论：关键词 `<关键词>`，目标词 `<A>`，排除词 `<B>`，评论 `<N>` 条 | 改 `reply.owner_comment` → 跑 → 还原 |
| 点 `<N>` 个赞 + 评论 `<M>` 条（频道：`<频道>`） | 跑 like → 手工切频道 → 跑 reply |
| 给这篇笔记发条评论「`<文案>`」 | BrowserMCP 打开链接 → 手工逐字输入 |
| 先讲一下 `<N>` 条评论的流程 | 只讲流程/摊开配置，等确认后执行 |
| 报告呢 / 点了没 / 报告在哪 | 读 `output/*.xlsx`，回表格 + 绝对路径 |

## 关键注意

- **`like.count` 是新赞目标数**，不是尝试次数（已赞自动跳过）
- **`reply.py` 不切频道**：频道必须先用 execute_js 点 tab（`location.href` 含 `homefeed.<频道>.v3`）
- `reply.owner_comment`：`owner_markers` 命中=目标、`dealer_markers` 命中=跳过；同作者只评一次（done_file 跨运行去重）；`require_owner=false` = 无目标限制
- 搜索链接必须带 `xsec_token`，脚本已处理；卡片少是时序问题，别怀疑反爬，别加 reload hack
- 文案写入 config 用 UTF-8；日志崩溃时如实说"没存上"，不要编造路径


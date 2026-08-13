"""小红书关键词搜索 - 配置驱动版（仅搜索结果卡片层）

P1 借鉴项 #8（本地历史归档若存在，参考 @archive/docs/research-mc-borrowing-detail.md §#8）。
DOM 路径不调小红书签名，工作方式借鉴自 历史研究脚本（js_cards 提取方式）。

⚠️ 2026-08 改版后回退：
  小红书搜索结果点击不再跳到带 token 的详情页，INITIAL_STATE 也只注 undefined 占位
  所以放弃详情页提取（之前尝试 INITIAL_STATE 兜底完全失败 — 见实测 commit 9c079a0）
  改为只抓搜索结果层（title / author / date / likes / href），这正是 历史研究脚本 的工作方式

复用 collect.py 的基础设施：
  - send_cmd: tenacity retry 包好的 MCP 调用（@retry 3 次 + wait_fixed 2s，仅 MCPDownError 触发）

输入：config.json 的 search 节
输出：Excel 三个 sheet（成功笔记 + 过滤明细 + 失败明细）
"""
import urllib.request, urllib.parse, json, time, random, re, functools, os, sys
from datetime import datetime, timedelta
import pandas as pd

from collect import send_cmd  # tenacity retry 包好的 MCP 调用
from exceptions import MCPDownError  # send_cmd 重试耗尽后抛出，主循环按关键词兜底
from privacy import mask_nickname, anonymize_user_id
from cover_utils import load_cover_config, download_cover, cover_hyperlink
from sort_utils import click_sort, SORT_HANDLERS  # P1.30 排序工具（reply.py 同模块共用）
# 注：小红书 2026-08 改版后，搜索结果点击不再跳到带 token 的详情页，
# INITIAL_STATE 也只注 undefined 占位，所以不进详情页（参考 .local/tmp/历史研究脚本 工作方式）
# extract_note / EXTRACT_JS / detect_exception_class 等保留在 collect.py 以备未来详情页能力恢复

print = functools.partial(print, flush=True)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

with open('config.json', 'r', encoding='utf-8') as f:
    CFG = json.load(f)

SEARCH = CFG.get('search', {})
KEYWORDS = SEARCH.get('keywords', [])
CUTOFF_DAYS = SEARCH.get('cutoff_days', 90)
MIN_LIKES = SEARCH.get('min_likes', 0)
MAX_LIKES = SEARCH.get('max_likes', None)  # 新场景：10赞以下 — 仅当配置时启用
EXCLUDE_SUBSTRINGS = SEARCH.get('exclude_author_substrings', [])
DEALER_PATTERNS = SEARCH.get('dealer_patterns', [])
MAX_NOTES_PER_KW = SEARCH.get('max_notes_per_keyword', 50)
# 高赞过滤会砍掉大量卡片，给到 6 倍余量以冲破默认排序的卡片池
COLLECT_LIMIT = int(MAX_NOTES_PER_KW * 6)
OUTPUT_FILE = SEARCH.get('output_file', 'output/xhs-search.xlsx')
# 输出文件名加时间戳，防止覆盖
_ts = datetime.now().strftime('%Y%m%d-%H%M%S')
_SEARCH_OUTPUT = OUTPUT_FILE.replace('.xlsx', f'-{_ts}.xlsx')
SCROLL_PAUSE = SEARCH.get('scroll_pause_range', [2.5, 4.0])
KEYWORD_GAP = SEARCH.get('keyword_gap_range', [25, 45])
HTTP_TIMEOUT = SEARCH.get('http_timeout', 60)

PRIVACY = CFG.get('privacy', {})
DO_MASK = PRIVACY.get('mask_nickname', False)
DO_ANONYMIZE = PRIVACY.get('anonymize_user_id', False)

# 封面下载：复用 reply / like 节的 download_covers 配置（已配置 true + output/covers）
_COVER_CFG = load_cover_config(CFG.get('like', {}))


# === 搜索结果卡片 DOM 提取 ===
# 借鉴并提炼自 历史研究脚本（js_cards 提取方式） 第 53-81 行的 js_cards。
# - 限定在视口内（rect.top ∈ [-50, viewH+100]），参考 try_click_filtered
# - 行模式：4 行 = title/author/date/likes；3 行 = (无标题) / author/date/likes
# - isDateLine 兼容"刚刚/今天/昨天/N 分钟前/N 天前/YYYY-MM-DD/MM-DD"全格式
SEARCH_CARDS_JS = '''
    (() => {
        const items = document.querySelectorAll('section.note-item');
        const results = [];
        const viewH = window.innerHeight || document.documentElement.clientHeight || 800;
        // P1.13 修复：兼容"昨天 10:16" / "今天 14:30" 等带具体时间的日期行
        const isDateLine = (s) => /^(刚刚|今天|昨天|\\d+\\s*(分钟|小时|天|周|月)前|\\d{4}-\\d{1,2}-\\d{1,2}|\\d{1,2}-\\d{1,2})(?:\\s+\\d{1,2}:\\d{2})?$/.test(s);
        items.forEach((item, idx) => {
            const rect = item.getBoundingClientRect();
            if (rect.top < -50 || rect.top > viewH + 100) return;
            const link = item.querySelector('a[href*="/explore/"]');
            if (!link) return;
            const href = link.href || '';
            const idMatch = href.match(/\\/explore\\/([a-f0-9]+)/);
            if (!idMatch) return;
            const fullText = (item.innerText || '').trim();
            const lines = fullText.split(/\\n+/).map(s => s.trim()).filter(Boolean);
            let title = '', author = '', dateStr = '', likeText = '';
            if (lines.length >= 4 && isDateLine(lines[2])) {
                title = lines[0]; author = lines[1]; dateStr = lines[2]; likeText = lines[3];
            } else if (lines.length >= 3 && isDateLine(lines[1])) {
                title = '(无标题)'; author = lines[0]; dateStr = lines[1]; likeText = lines[2];
            } else {
                title = lines[0] || ''; author = lines[1] || ''; dateStr = lines[2] || ''; likeText = lines[3] || '';
            }
            results.push({
                index: idx, id: idMatch[1], href,
                title, author, date_str: dateStr, like_text: likeText,
                // 取封面 URL：取首个非头像的 img（cover 在前，author-avatar 在后）
                cover_url: (item.querySelector('a.cover img') || {}).src || '',
                // 真实详情页 URL：cover <a> 带 xsec_token + xsec_source，
                // 把它组装成 /explore/<id>?xsec_token=...&xsec_source=...
                detail_url: (() => {
                    const cl = item.querySelector('a.cover');
                    if (!cl || !cl.href) return href;
                    const params = cl.href.match(/xsec_token=[^&]+|xsec_source=[^&]+/g);
                    if (!params) return href;
                    return `https://www.xiaohongshu.com/explore/${idMatch[1]}?` + params.join('&');
                })(),
            });
        });
        return results;
    })()
'''


# 排序切换逻辑已抽离到 sort_utils.py（详见 click_sort / SORT_HANDLERS）


_XHS_TAB_ID = None  # 小红书标签页 ID，navigate_keyword 设置后全程复用

def _ensure_xhs_tab():
    """确保 BrowserMCP 活动标签页是小红书（不在用户自己的标签页上跑 JS）。

    BrowserMCP 的 execute_js 默认在活动标签页执行，如果用户在运行期间切换了
    标签页，JS 会在错误的页面上执行。用 switch_tab 切回小红书标签页。
    """
    global _XHS_TAB_ID
    tabs = (send_cmd('list_tabs', {}, timeout=HTTP_TIMEOUT) or {}).get('result') or []
    xhs_tabs = [t for t in tabs if 'xiaohongshu.com' in (t.get('url') or '')]
    if not xhs_tabs:
        return
    # 选已记住的那个，或第一个
    target = xhs_tabs[0]
    if _XHS_TAB_ID:
        for t in xhs_tabs:
            if t['id'] == _XHS_TAB_ID:
                target = t
                break
    _XHS_TAB_ID = target['id']
    # 切换到小红书标签页（如果不是活动标签页）
    if not target.get('active'):
        send_cmd('switch_tab', {'tabId': _XHS_TAB_ID}, timeout=HTTP_TIMEOUT)
    return _XHS_TAB_ID


def exec_js(code):
    """包一层：拿 send_cmd 返回的 result 字段（dict / list / null）。

    执行前确保在小红书标签页，避免 BrowserMCP 在用户自己的标签页上跑 JS。
    """
    _ensure_xhs_tab()
    r = send_cmd('execute_js', {'code': code}, timeout=HTTP_TIMEOUT)
    return (r or {}).get('result')


def apply_sort():
    """按 config.search.sort_by 切换排序。失败不阻抓取（综合排序也照样能跑）。"""
    sort_by = SEARCH.get('sort_by', 'comprehensive')
    try:
        # exec_js 自带 _ensure_xhs_tab 守卫 + envelope 解包 + HTTP_TIMEOUT
        res = click_sort(exec_js, sort_by=sort_by)
        print(f'  🔀 排序切换: {res}')
        return res
    except MCPDownError as e:
        print(f'  ⚠️ 切换排序失败（sort_by={sort_by}）: {e}，继续按页面默认排序抓取')
        return {'clicked': False, 'error': str(e)}


def parse_date(s):
    """搜索卡片上的相对/绝对日期字符串 → datetime。参考 历史研究脚本.py parse_date。"""
    s = (s or '').strip()
    now = datetime.now()
    if not s:
        return None
    if s in ('刚刚', '今天') or s.startswith('今天 '):
        return now
    if s == '昨天' or s.startswith('昨天 '):
        return now - timedelta(days=1)
    for pat, unit in [(r'^(\d+)\s*天前$', 'd'), (r'^(\d+)\s*小时前$', 'h'), (r'^(\d+)\s*分钟前$', 'm'),
                      (r'^(\d+)\s*周前$', 'w'), (r'^(\d+)\s*月前$', 'M')]:
        m = re.match(pat, s)
        if m:
            n = int(m.group(1))
            if unit == 'w':
                return now - timedelta(weeks=n)
            elif unit == 'M':
                return now - timedelta(days=n * 30)
            return now - (timedelta(days=n) if unit == 'd'
                          else timedelta(hours=n) if unit == 'h'
                          else timedelta(minutes=n))
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r'^(\d{1,2})-(\d{1,2})$', s)
    if m:
        try:
            d = datetime(now.year, int(m.group(1)), int(m.group(2)))
            # 跨年：当前日期早于解析出的月日时，回退一年
            # 例：1 月解析 "12-31" → 上一年 12-31
            if d > now:
                d = d.replace(year=now.year - 1)
            return d
        except ValueError:
            return None
    return None


def parse_likes(s):
    """'赞' / '1.2万' / '1234' → int。参考 collect.py try_click_filtered parseCount。"""
    s = (s or '').strip()
    if not s or s == '赞':
        return 0
    if '万' in s:
        try:
            return round(float(s.replace('万', '')) * 10000)
        except ValueError:
            return 0
    m = re.search(r'\d+', s)
    return int(m.group(0)) if m else 0


def filter_cards(cards):
    """过滤搜索结果：cutoff_days / exclude_author_substrings / dealer_patterns。

    返回 (keep, filtered):
      keep:    通过过滤的卡片（已补 likes 字段；顺序 = XHS 页面顺序，信任页面切好的排序）
      filtered: 被过滤掉的卡片（含 过滤原因 字段，供后续 review）

    不在 Python 端排序 — 切了「最新」chip 后页面就按时间降序出卡，
    再在 Python 端 sort 会和 XHS 内部顺序打架。日期解析仍保留给过滤明细对账。
    """
    cutoff = datetime.now() - timedelta(days=CUTOFF_DAYS)
    keep, filtered = [], []
    for c in cards:
        author = c.get('author', '') or ''
        date_str = c.get('date_str', '') or ''
        likes_val = parse_likes(c.get('like_text', ''))
        dt = parse_date(date_str)
        reasons = []
        author_lc = author.lower()
        if dt is not None and dt < cutoff:
            reasons.append(f'超过 {CUTOFF_DAYS} 天')
        if any(sub in author for sub in EXCLUDE_SUBSTRINGS):
            reasons.append(f'作者含排除词 {EXCLUDE_SUBSTRINGS}')
        dealer_hits = [p for p in DEALER_PATTERNS if p.lower() in author_lc]
        if dealer_hits:
            reasons.append('经销商（命中：' + ' / '.join(dealer_hits) + '）')
        if likes_val < MIN_LIKES:
            reasons.append(f'点赞数 {likes_val} < {MIN_LIKES}')
        if MAX_LIKES is not None and likes_val > MAX_LIKES:
            reasons.append(f'点赞数 {likes_val} > {MAX_LIKES}')
        rec = dict(c)
        rec['likes'] = likes_val
        if reasons:
            rec['过滤原因'] = ' | '.join(reasons)
            filtered.append(rec)
        else:
            keep.append(rec)
    # 不在 Python 端排序 — 信任 XHS 页面顺序（切了「最新」chip 就按时间降序，
    # 切了「综合」就按 XHS 算法）。保留 seen 字典插入顺序，避免双排序打架。
    return keep, filtered


def navigate_keyword(kw):
    """导航到搜索结果页（复用小红书标签页，不新建）。

    标签页纪律（用户约束："在同一个标签中操作"）：
    BrowserMCP 的 navigate 每次 chrome.tabs.create 新标签页，
    所以优先复用已有的小红书标签页（用 location.href 导航），
    没有时才 navigate 新建。多轮运行只保留 1 个小红书标签页。
    """
    global _XHS_TAB_ID
    url = (f'https://www.xiaohongshu.com/search_result?keyword={urllib.parse.quote(kw)}'
           f'&source=web_explore_feed&type=51')
    try:
        tabs = (send_cmd('list_tabs', {}, timeout=HTTP_TIMEOUT) or {}).get('result') or []
        xhs_tabs = [t for t in tabs if 'xiaohongshu.com' in (t.get('url') or '')]
        if xhs_tabs:
            # 复用第一个小红书标签页，关掉多余的小红书标签页
            for t in xhs_tabs[1:]:
                send_cmd('close_tab', {'tabId': t['id']}, timeout=HTTP_TIMEOUT)
            _XHS_TAB_ID = xhs_tabs[0]['id']
            # 切换到该标签页 + location.href 导航（不新建标签页）
            send_cmd('switch_tab', {'tabId': _XHS_TAB_ID}, timeout=HTTP_TIMEOUT)
            send_cmd('execute_js', {'code': f'location.href = "{url}"', 'tabId': _XHS_TAB_ID}, timeout=HTTP_TIMEOUT)
            time.sleep(6)
            return url
    except Exception as e:
        print(f'  ⚠️ 标签页复用失败，走新建: {e}')
    # 没有小红书标签页，navigate 新建
    send_cmd('navigate', {'url': url}, timeout=HTTP_TIMEOUT)
    time.sleep(6)
    return url


def collect_cards(kw):
    """滚动搜索结果页 + 抓取所有可见卡片（按 id 去重）。

    默认：8 轮 800-1200px 滚动（拟人浏览节奏），不主动 reload，
    让浏览器走正常的 SPA 增量加载。

    翻屏模式（search.screen_scroll.enabled）：
      阶段 1：随机翻 first_rounds_range 屏（1 屏 = 一个视口高度，拟人节奏）
      刷新（location.reload()，人类"刷一刷换一批 feed"）后
      阶段 2：再随机翻 second_rounds_range 屏。
      每屏后增量收集，两阶段按 id 全局去重。
    """
    screen = SEARCH.get('screen_scroll', {})
    seen = {}

    def grab():
        batch = exec_js(SEARCH_CARDS_JS) or []
        for c in batch:
            cid = c.get('id')
            if cid and cid not in seen:
                seen[cid] = c

    def scroll_one_screen():
        """翻一屏并等待虚拟列表加载新卡片。

        小红书搜索结果用虚拟列表：视口外卡片被 DOM 移除、新卡片异步加载。
        滚动后固定等虚拟列表加载时间（拟人节奏：人看屏幕需要 1-2s），
        grab() 去重后比较新增数量判断到底。
        """
        vh = exec_js('window.innerHeight || 800') or 800
        send_cmd('scroll', {'direction': 'down', 'pixels': vh}, timeout=HTTP_TIMEOUT)
        # 虚拟列表加载需要时间，固定等待（人类翻页后会停 1-3s）
        time.sleep(random.uniform(2.0, 3.0))

    # 先抓首屏（不滚动），避免首屏卡片漏采
    grab()

    if screen.get('enabled'):
        # 阶段 1：随机翻 7-15 屏
        r1 = random.randint(*screen.get('first_rounds_range', [7, 15]))
        for _ in range(r1):
            if len(seen) >= COLLECT_LIMIT:
                break
            scroll_one_screen()
            grab()
        print(f'  🖥️ 阶段1 翻 {r1} 屏，累计 {len(seen)} 张不重复卡片')
        if screen.get('refresh_between', True):
            # 刷新换一批 feed（拟人：刷新页面重看）
            exec_js('location.reload(); true')
            time.sleep(6)
            # 阶段 2：随机翻 5-8 屏
            r2 = random.randint(*screen.get('second_rounds_range', [5, 8]))
            for _ in range(r2):
                if len(seen) >= COLLECT_LIMIT:
                    break
                scroll_one_screen()
                grab()
            print(f'  🖥️ 阶段2 翻 {r2} 屏，累计 {len(seen)} 张不重复卡片')
        else:
            # 不刷新：一直往下翻，连续 3 屏无新卡片视为到底（用户原话："不要刷新，一直往下翻"）
            stale = 0
            while len(seen) < COLLECT_LIMIT and stale < 3:
                before = len(seen)
                scroll_one_screen()
                grab()
                stale = stale + 1 if len(seen) == before else 0
            print(f'  🖥️ 不刷新深翻结束（连续 {stale} 屏无新卡），累计 {len(seen)} 张不重复卡片')
    else:
        for rnd in range(8):
            if len(seen) >= COLLECT_LIMIT:
                break
            send_cmd('scroll', {'direction': 'down', 'pixels': random.randint(800, 1200)},
                     timeout=HTTP_TIMEOUT)
            time.sleep(random.uniform(*SCROLL_PAUSE))
            grab()
    return list(seen.values())


def record_fail(seq, kw, note_id, reason):
    """统一记入失败表（task brief 指定 4 列：频道/序号/笔记ID/失败原因）。"""
    failed_notes.append({
        '频道': kw,
        '序号': seq,
        '笔记ID': note_id,
        '失败原因': reason,
    })


all_results = []
failed_notes = []
_all_filtered = []  # 各关键词过滤掉的卡片（带过滤原因）
ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def main():
    # === 主循环：每个关键词一次搜索 ===
    for kw in KEYWORDS:
        print(f'\n{"="*60}')
        print(f'🔍 关键词: {kw}')
        print(f'{"="*60}')

        # 1. 导航到搜索结果页
        try:
            url = navigate_keyword(kw)
            print(f'  ✅ 已打开搜索页: {url[:60]}...')
        except MCPDownError as e:
            print(f'  ❌ 关键词页导航失败: {e}')
            record_fail(0, kw, '', f'导航失败: {e}')
            continue

        # 1.5 按配置切换排序（latest=点击「最新」chip；comprehensive=保持综合，不做点击）
        apply_sort()

        # 2. 滚动 + 收集卡片
        try:
            cards = collect_cards(kw)
            print(f'  📦 抓取到 {len(cards)} 张不重复卡片')
        except MCPDownError as e:
            print(f'  ❌ 抓取卡片失败: {e}')
            record_fail(0, kw, '', f'抓取失败: {e}')
            continue

        # 3. 过滤
        keep, filtered = filter_cards(cards)
        print(f'  🔎 过滤后保留 {len(keep)} 条 / 过滤 {len(filtered)} 条')
        # 累计过滤明细（带关键词列）
        for f in filtered:
            f['关键词'] = kw
        _all_filtered.extend(filtered)

        # 4. 直接用搜索结果卡片数据填 Excel（不进详情页）
        # 借鉴 历史研究脚本 的工作方式：小红书 2026-08 改版后，
        # 搜索结果点击不再跳到带 token 的详情页，INITIAL_STATE 也只注 undefined 占位，
        # 所以放弃详情页提取，专注搜索结果层（title/author/date/likes/href/cover_url）
        for seq, card in enumerate(keep[:MAX_NOTES_PER_KW], start=1):
            note_id = card.get('id', '')
            author = card.get('author', '') or ''
            title = card.get('title', '') or ''
            cover_url = card.get('cover_url', '') or ''
            # 下载封面（幂等，已存在则跳过）
            cover_local = download_cover(cover_url, note_id, _COVER_CFG) if cover_url else ''
            print(f'\n  📱 [{kw}] #{seq} {author[:18]} | {title[:25]}')
            print(f'    ✅ {author} | {title[:25]} | ❤️{card.get("likes", 0)} | {card.get("date_str", "")} | 🖼 {bool(cover_local)}')

            # 4d. 写入成功表（字段对齐 like.py / collect.py + 封面复用 cover_hyperlink）
            all_results.append({
                '关键词': kw,
                '序号': seq,
                '笔记ID': note_id,
                '笔记作者': author,
                '笔记作者_脱敏': mask_nickname(author) if DO_MASK else '',
                '笔记作者_哈希': anonymize_user_id(author) if DO_ANONYMIZE else '',
                '笔记标题': title,
                '发布日期': card.get('date_str', ''),
                '点赞数': card.get('likes', 0),
                # 复用 like.py / reply.py 的字段命名（cover_utils.build_log_entry）：
                '链接': card.get('detail_url') or card.get('href', ''),
                '封面图片': cover_url,
                '封面图片_本地': cover_hyperlink(cover_local),
                '封面路径': cover_local,  # 相对路径，便于程序读取
                '抓取时间': ts,
            })

        # 5. 关键词间隔（防风控）
        gap = random.uniform(*KEYWORD_GAP)
        print(f'\n  ⏱️ 关键词 {kw} 完成，等 {gap:.0f}s 进下一个...')
        time.sleep(gap)

    # === 成表 ===
    print(f'\n{"="*60}')
    print(f'💾 全部完成！保留 {len(all_results)} 条，过滤 {len(_all_filtered)} 条，失败 {len(failed_notes)} 条')
    print(f'{"="*60}')

    out_dir = os.path.dirname(_SEARCH_OUTPUT)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    _save_path = _SEARCH_OUTPUT
    # P1.14 修复：过滤明细无条件写盘（与失败明细同等待遇），保证全过滤 / 全空场景下结构稳定
    _filtered_cols = ['author', 'date_str', 'href', 'id', 'index', 'like_text', 'title', 'likes', '过滤原因', '关键词']
    try:
        with pd.ExcelWriter(_save_path, engine='openpyxl') as writer:
            if all_results:
                pd.DataFrame(all_results).to_excel(writer, sheet_name='成功笔记', index=False)
            # 过滤明细无条件写盘：空场景下保留 sheet 结构，便于后续对账
            pd.DataFrame(_all_filtered, columns=_filtered_cols).to_excel(writer, sheet_name='过滤明细', index=False)
            # 失败明细总是写盘：record_fail() 填的数据必须落表，不再丢失；
            # 同时保证全失败 / 全空场景下 Excel 至少有一个 sheet，避免零 sheet 崩溃
            # （IndexError: At least one sheet must be visible）
            # 列与 record_fail 构造一致：频道 / 序号 / 笔记ID / 失败原因
            pd.DataFrame(failed_notes, columns=['频道', '序号', '笔记ID', '失败原因']).to_excel(writer, sheet_name='失败明细', index=False)
    except PermissionError:
        _ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        _base, _ext = os.path.splitext(_SEARCH_OUTPUT)
        _save_path = f'{_base}_{_ts}{_ext}'
        print(f'⚠️ 文件被占用，改存到: {_save_path}')
        with pd.ExcelWriter(_save_path, engine='openpyxl') as writer:
            if all_results:
                pd.DataFrame(all_results).to_excel(writer, sheet_name='成功笔记', index=False)
            pd.DataFrame(_all_filtered, columns=_filtered_cols).to_excel(writer, sheet_name='过滤明细', index=False)
            pd.DataFrame(failed_notes, columns=['频道', '序号', '笔记ID', '失败原因']).to_excel(writer, sheet_name='失败明细', index=False)

    print(f'✅ 已保存到: {os.path.abspath(_save_path)}')
    if all_results:
        print('\n── 保留笔记 ──')
        for row in all_results:
            print(f'  [{row["关键词"]}] {row["笔记作者"][:18]} | {row["笔记标题"][:25]}'
                  f' | ❤️{row["点赞数"]} | {row["发布日期"]}')
    if _all_filtered:
        print('\n── 过滤明细 ──')
        for row in _all_filtered:
            print(f'  [{row.get("关键词", kw)}] {row.get("author", "")[:18]} | {row.get("过滤原因", "")}')
    if failed_notes:
        print('\n── 失败明细 ──')
        for row in failed_notes:
            note_id = row.get('笔记ID') or '(无)'
            print(f'  [{row.get("频道", "")}] #{row.get("序号", "")} | {note_id} | {row.get("失败原因", "")}')


if __name__ == '__main__':
    main()

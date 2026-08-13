"""小红书拟人点赞 - 配置驱动版
配置见 config.json 的 like 节点。
已赞检测：use href 为 #liked 表示已赞（like-active 类是默认类，不能作判断）。
已赞过的笔记跳过不计数，直到点满 count 个新赞或达到 max_attempts。
"""
import socket, urllib.request, json, time, random, functools, os, sys
import pandas as pd
from privacy import mask_nickname, anonymize_user_id
from exceptions import MCPDownError
from exception_map import EXCEPTION_MAP
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type
from cover_utils import load_cover_config, download_cover, cover_hyperlink, EXTRACT_INFO_JS, build_log_entry

print = functools.partial(print, flush=True)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

with open('config.json', 'r', encoding='utf-8') as f:
    CFG = json.load(f)

L = CFG['like']
B = CFG['browse']
PRIVACY = CFG.get('privacy', {})
DO_MASK = PRIVACY.get('mask_nickname', False)
DO_ANONYMIZE = PRIVACY.get('anonymize_user_id', False)
LIKE_CLOSE_DELAY = L.get('close_modal_delay_range', [1.0, 2.5])
LIKE_BACK_DELAY = L.get('back_delay_range', [0.8, 2.0])
LIKE_POLL_INTERVAL = L.get('modal_poll_interval_range', [0.3, 1.0])
LIKE_POLL_MAX = L.get('modal_poll_max', 20)
LIKE_SCROLL_TOP = L.get('scroll_top_attempts', 8)
LIKE_REFRESH_RETRY = L.get('refresh_retry_max', 10)
CHANNEL_SWITCH_DELAY = B.get('channel_switch_delay_range', [2.0, 3.5])
# P1.15 改造：搜索关键词点赞任务列表（与 channels 并列，互不干扰）
SEARCH_KEYWORDS = L.get('search_keywords', [])
# P1.17 改造：封面下载改用 cover_utils 共享模块（cover_utils.load_cover_config 内部读 download_covers/cover_dir/cover_timeout/cover_max_size_mb）
COVER_CFG = load_cover_config(L)

def rand(r):
    return random.uniform(r[0], r[1])

@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type(MCPDownError),
    reraise=True,
)
def send_cmd(action, params, timeout=None):
    """调 BrowserMCP；MCP 抖动时自动重试 3 次。timeout 默认走 B['http_timeout']。"""
    req = urllib.request.Request(
        'http://localhost:12800/exec',
        data=json.dumps({'action': action, 'params': params}).encode(),
        headers={'Content-Type': 'application/json'}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout or B['http_timeout'])
    except urllib.error.URLError as e:
        raise MCPDownError(f'MCP unreachable: {e}') from e
    except (ConnectionResetError, ConnectionAbortedError) as e:
        raise MCPDownError(f'MCP connection dropped: {e}') from e
    except (socket.timeout, TimeoutError) as e:
        raise MCPDownError(f'MCP timeout: {e}') from e
    return json.loads(resp.read().decode())

# 浏览器端异常探测：返回 'CAPTCHA' / 'NOTE_GONE' / null
# 与 collect.py / reply.py 复用同一段 JS（与 EXCEPTION_MAP key 对齐）
DETECT_CLASS_JS = '''
    (() => {
        const t = document.body ? document.body.innerText : '';
        if (t.includes('请通过验证')) return 'CAPTCHA';
        if (t.includes('页面不存在') || t.includes('该笔记已被删除') || t.includes('该内容已被删除')) return 'NOTE_GONE';
        return null;
    })()
'''


def detect_exception_class():
    """浏览器内探测当前异常类，返回 'CAPTCHA' / 'NOTE_GONE' / None。
    网络异常 IP_BLOCKED / MCP_DOWN 由 send_cmd 层识别。
    """
    try:
        r = send_cmd('execute_js', {'code': DETECT_CLASS_JS})
    except MCPDownError as e:
        print(f'  ⚠️ detect 调用失败：{e}')
        return 'MCP_DOWN'
    return (r or {}).get('result')


def is_modal_open():
    try:
        r = send_cmd('execute_js', {
            'code': '(() => { const m = document.querySelector(".note-detail-mask"); return !!m && m.getBoundingClientRect().width > 300; })()'
        })
        return bool(r.get('result'))
    except:
        return False

def get_like_state():
    """读 use href：#like=未赞 #liked=已赞"""
    return send_cmd('execute_js', {
        'code': '''
            (() => {
                const mask = document.querySelector('.note-detail-mask');
                if (!mask) return { error: 'no modal' };
                const w = mask.querySelector('.engage-bar .like-wrapper');
                if (!w) return { error: 'no wrapper' };
                const use = w.querySelector('use');
                const c = w.querySelector('.count');
                const href = use ? (use.getAttribute('xlink:href') || use.getAttribute('href')) : '';
                return { href, count: c ? c.innerText.trim() : '', liked: href === '#liked' };
            })()
        '''
    }).get('result') or {}

def pick_note_and_click(exclude_ids):
    """找卡片并点击。P1.15 改造：兼容搜索结果页 (/search_result/) 链接。"""
    resp = send_cmd('execute_js', {
        'code': f'''
            (() => {{
                const exclude = new Set({json.dumps(exclude_ids)});
                const minLikes = {CFG['min_likes']};
                if (document.querySelector('.note-detail-mask')) return {{ clicked: false }};
                const parseCount = (s) => {{
                    s = (s || '').trim();
                    if (!s || s === '赞') return 0;
                    if (s.includes('万')) return Math.round(parseFloat(s) * 10000);
                    const m = s.match(/\\d+/);
                    return m ? parseInt(m[0]) : 0;
                }};
                const candidates = [];
                for (const card of document.querySelectorAll('section.note-item')) {{
                    let link = null;
                    // P1.15 改造：兼容 /explore/（feed 详情页）和 /search_result/（搜索结果详情页）两种链接
                    for (const l of card.querySelectorAll('a[href*="/explore/"], a[href*="/search_result/"]')) {{
                        if (window.getComputedStyle(l).display !== 'none' && l.innerText.trim()) {{ link = l; break; }}
                    }}
                    if (!link) continue;
                    const rect = card.getBoundingClientRect();
                    if (rect.top < 0 || rect.top > window.innerHeight) continue;
                    const likeEl = card.querySelector('.like-wrapper .count') || card.querySelector('.like-wrapper');
                    const likes = parseCount(likeEl ? likeEl.innerText : '');
                    if (likes < minLikes) continue;
                    const m = link.href.match(/(?:explore|search_result)\\/([a-f0-9]+)/);
                    if (m && exclude.has(m[1])) continue;
                    candidates.push({{ link, likes, id: m ? m[1] : '', text: link.innerText.substring(0, 40) }});
                }}
                if (candidates.length > 0) {{
                    const pick = candidates[Math.floor(Math.random() * candidates.length)];
                    pick.link.click();
                    return {{ clicked: true, text: pick.text, id: pick.id, likes: pick.likes }};
                }}
                return {{ clicked: false }};
            }})()
        '''
    })
    return (resp or {}).get('result', {}) or {}

def switch_channel(name):
    """点击频道 tab 切换"""
    safe_name = json.dumps(name)
    r = send_cmd('execute_js', {
        'code': f'''
            (() => {{
                for (const tab of document.querySelectorAll('[class*="channel-content"], [class*="channel-item"]')) {{
                    if (tab.innerText.trim() === {safe_name}) {{ tab.click(); return true; }}
                }}
                return false;
            }})()
        '''
    })
    return bool(r.get('result'))


def navigate_to_search(keyword):
    """P1.15 新增：导航到搜索结果页。复用 search.py 的 URL 格式。"""
    import urllib.parse
    url = (f'https://www.xiaohongshu.com/search_result?keyword={urllib.parse.quote(keyword)}'
           f'&source=web_explore_feed&type=51')
    send_cmd('navigate', {'url': url})
    # 等 SPA 首屏加载（避开空卡片）
    time.sleep(6)


like_log = []
used_ids = []

# 输出文件名加时间戳，防止覆盖
import datetime
_ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
_output_file = L['output_file'].replace('.xlsx', f'-{_ts}.xlsx')
# P1.15 改造：搜索任务用独立文件名（带 search 后缀），便于区分频道任务和搜索任务
_search_output_file = L['output_file'].replace('.xlsx', f'-search-{_ts}.xlsx')
# 当前 task 的输出文件指针（在 main 循环中切换）
_current_output_file = [_output_file]


def _save_to(file_path):
    """内部 helper：把当前 like_log 写到指定文件。"""
    if like_log:
        pd.DataFrame(like_log).to_excel(file_path, index=False)


def save_log():
    """增量保存日志，每次操作后调用。P1.15：写到当前 task 的输出文件。"""
    _save_to(_current_output_file[0])


def main():
    total_liked = 0   # 全局成功赞数
    attempt = 0
    since_refresh = 0  # 距上次页面刷新以来点的赞数
    next_refresh_at = random.randint(*L['refresh_every_range'])

    # P1.15 改造：把 channels 和 search_keywords 展开为统一 tasks 列表
    # task 结构: {type: 'channel'|'search', label, count, [keyword]}
    tasks = []
    for ch in L.get('channels', []):
        tasks.append({'type': 'channel', 'label': ch['name'], 'count': ch['count']})
    for kw in SEARCH_KEYWORDS:
        tasks.append({'type': 'search', 'label': f"搜索:{kw['keyword']}", 'count': kw['count'], 'keyword': kw['keyword']})

    total_target = sum(t['count'] for t in tasks)
    if total_target == 0:
        print('⚠️ like.channels 和 like.search_keywords 都为空，无任务可跑')
        return

    summary_parts = [f"{t['label']}{t['count']}" for t in tasks]
    print(f'目标: {total_target} 个新赞（' + ' + '.join(summary_parts) + f'）| 每 {next_refresh_at} 个赞刷新页面')

    # CAPTCHA / IP_BLOCKED 一旦触发立即终止当前 task（外层 for task 看到 flag 后 break）
    captcha_triggered = False

    for task in tasks:
        if captcha_triggered:
            # 上一 task 已遇 CAPTCHA，跳过后续所有 task
            break
        ch_name = task['label']
        ch_target = task['count']
        ch_liked = 0
        # P1.15 改造：每个 task 用独立输出文件（频道任务→标准文件，搜索任务→search 后缀文件）
        _current_output_file[0] = _search_output_file if task['type'] == 'search' else _output_file
        print(f'\n{"="*50}')
        print(f'📺 任务: {ch_name}（目标 {ch_target} 个赞）')
        print(f'{"="*50}')

        # 导航：根据 task 类型选择
        if is_modal_open():
            send_cmd('back', {})
            time.sleep(rand(LIKE_CLOSE_DELAY))
        if task['type'] == 'channel':
            if switch_channel(task['label']):
                print(f'  ✅ 已切换到 {task["label"]}')
                time.sleep(rand(CHANNEL_SWITCH_DELAY))
            else:
                # 第一次没找到 → 回首页，再选一次
                print(f'  ⚠️ 未找到 {task["label"]} tab，回首页重选...')
                send_cmd('navigate', {'url': 'https://www.xiaohongshu.com'})
                time.sleep(rand(CHANNEL_SWITCH_DELAY))
                if switch_channel(task['label']):
                    print(f'  ✅ 回首页后已切换到 {task["label"]}')
                    time.sleep(rand(CHANNEL_SWITCH_DELAY))
                else:
                    print(f'  ❌ 首页仍未找到 {task["label"]} tab，跳过该任务')
                    like_log.append({'频道': task['label'], '状态': '失败', '失败原因': f'找不到 {task["label"]} tab（含首页重试）'})
                    save_log()
                    continue
        elif task['type'] == 'search':
            print(f'  🔍 导航到搜索结果页: {task["keyword"]}')
            navigate_to_search(task['keyword'])

        # 重置 used_ids：每个任务独立去重（频道不重复，搜索结果不重复）
        task_used_ids = []

        while ch_liked < ch_target and attempt < L['max_attempts']:
            attempt += 1
            print(f'\n── [{ch_name}] {ch_liked}/{ch_target} | 全局 {total_liked}/{total_target}（第 {attempt} 次尝试）──')

            # 到阈值主动刷新页面，避免虚拟列表滚太深进入坏状态
            if since_refresh >= next_refresh_at:
                print(f'  🔄 已赞 {since_refresh} 个，主动刷新页面...')
                send_cmd('execute_js', {'code': 'location.reload()'})
                time.sleep(random.uniform(4, 6))
                since_refresh = 0
                next_refresh_at = random.randint(*L['refresh_every_range'])

            if is_modal_open():
                send_cmd('back', {})
                time.sleep(rand(LIKE_CLOSE_DELAY))

            # 拟人刷 feed
            for i in range(random.randint(*B['scrolls_per_attempt'])):
                send_cmd('scroll', {'direction': 'down', 'pixels': random.randint(*B['scroll_px_range'])})
                time.sleep(rand(B['scroll_pause_range']))

            # 找笔记
            cr = {}
            for scroll_try in range(CFG['max_scroll_attempts']):
                cr = pick_note_and_click(task_used_ids) or {}
                if cr.get('clicked'):
                    break
                send_cmd('scroll', {'direction': 'down', 'pixels': random.randint(*B['scroll_px_range'])})
                time.sleep(rand(B['scroll_pause_range']))
                # 每刷 8 屏还没找到，回顶部换一批
                if scroll_try > 0 and scroll_try % LIKE_SCROLL_TOP == 0:
                    print('  🔝 回顶部换一批...')
                    send_cmd('execute_js', {'code': 'window.scrollTo(0, 0)'})
                    time.sleep(random.uniform(2, 3))
            if not cr.get('clicked'):
                # 滚动恢复无效，刷新页面重试一次
                print('  🔄 刷新页面恢复...')
                send_cmd('execute_js', {'code': 'location.reload()'})
                time.sleep(random.uniform(4, 6))
                for _ in range(LIKE_REFRESH_RETRY):
                    cr = pick_note_and_click(task_used_ids) or {}
                    if cr.get('clicked'):
                        break
                    send_cmd('scroll', {'direction': 'down', 'pixels': random.randint(*B['scroll_px_range'])})
                    time.sleep(rand(B['scroll_pause_range']))
            if not (cr or {}).get('clicked'):
                print('  ❌ 找不到笔记')
                like_log.append({'频道': ch_name, '序号': attempt, '状态': '失败', '失败原因': '找不到笔记'})
                save_log()
                continue

            note_id = cr.get('id', '')
            task_used_ids.append(note_id)
            print(f'  👆 {cr.get("text")[:30]}（❤️{cr.get("likes")}）')

            # 等弹窗
            for _ in range(LIKE_POLL_MAX):
                if is_modal_open():
                    break
                time.sleep(rand(LIKE_POLL_INTERVAL))
            else:
                print('  ❌ 弹窗未打开')
                like_log.append({'频道': ch_name, '序号': attempt, '笔记ID': note_id, '状态': '失败', '失败原因': '弹窗未打开'})
                save_log()
                continue

            # 异常分级探测：CAPTCHA → 终止该频道（外层 for ch 用 flag 感知）；
            #               NOTE_GONE → 跳过本笔记；
            #               IP_BLOCKED / MCP_DOWN → 跳过本笔记，等重试衰减或人工干预
            # 借鉴 MediaCrawler media_platform/xhs/exception.py
            exception_class = detect_exception_class()
            if exception_class == 'CAPTCHA':
                label, action = EXCEPTION_MAP['CAPTCHA']
                print(f'  ⛔ {label}（{action}）')
                like_log.append({'频道': ch_name, '序号': attempt, '笔记ID': note_id, '点赞数': '', '状态': '失败', '失败原因': label})
                save_log()
                captcha_triggered = True
                break  # 跳出 while；for ch 顶部会检查 captcha_triggered 并 break
            elif exception_class == 'NOTE_GONE':
                label, action = EXCEPTION_MAP['NOTE_GONE']
                print(f'  ⏭️ {label}（{action}）')
                like_log.append({'频道': ch_name, '序号': attempt, '笔记ID': note_id, '点赞数': '', '状态': '失败', '失败原因': label})
                save_log()
                continue
            elif exception_class == 'MCP_DOWN':
                label, action = EXCEPTION_MAP[exception_class]
                print(f'  🚫 {label}（{action}）')
                like_log.append({'频道': ch_name, '序号': attempt, '笔记ID': note_id, '点赞数': '', '状态': '失败', '失败原因': label})
                save_log()
                continue

            info = send_cmd('execute_js', {'code': EXTRACT_INFO_JS}).get('result') or {}
            # P1.21: info JS 异常不再静默（gpt-5.6-sol finding #5）
            if info.get('error'):
                print(f'  ⚠️ info JS 异常: {info["error"]}')

            # 拟人阅读
            read_time = rand(L['read_time_range'])
            time.sleep(read_time)

            # 检查是否已赞
            before = get_like_state() or {}
            if not before or before.get('error'):
                # get_like_state 失败（如 modal 已关闭），安全起见跳过，避免误赞
                print(f'  ⚠️ get_like_state 失败（{before.get("error")}），跳过')
                like_log.append(build_log_entry(info, note_id, COVER_CFG, extra_fields={
                    '频道': ch_name, '序号': attempt,
                    '笔记作者_脱敏': mask_nickname(info.get('author')) if DO_MASK else '',
                    '笔记作者_哈希': anonymize_user_id(info.get('author', '')) if DO_ANONYMIZE else '',
                    '状态': '跳过', '失败原因': f'get_like_state 失败: {before.get("error")}'
                }))
                save_log()
                send_cmd('back', {})
                time.sleep(rand(LIKE_BACK_DELAY))
                continue
            if before.get('liked'):
                print(f'  ⏭️ 已赞过（{info.get("author")}），跳过不计数')
                like_log.append(build_log_entry(info, note_id, COVER_CFG, extra_fields={
                    '频道': ch_name, '序号': attempt,
                    '笔记作者_脱敏': mask_nickname(info.get('author')) if DO_MASK else '',
                    '笔记作者_哈希': anonymize_user_id(info.get('author', '')) if DO_ANONYMIZE else '',
                    '点赞数': before.get('count', ''),
                    '状态': '跳过', '失败原因': '已赞过'
                }))
                save_log()
                send_cmd('back', {})
                time.sleep(rand(LIKE_BACK_DELAY))
                continue

            # 点赞
            send_cmd('execute_js', {
                'code': '(() => { const w = document.querySelector(".note-detail-mask .engage-bar .like-wrapper"); if (w) w.click(); })()'
            })
            time.sleep(random.uniform(1.0, 2.0))

            # 验证
            after = get_like_state()
            ok = after.get('liked') and not before.get('liked')
            if ok:
                ch_liked += 1
                total_liked += 1
                since_refresh += 1
                print(f'  ❤️ 点赞成功（{before.get("count")} → {after.get("count")}）| 频道 {ch_liked}/{ch_target} 全局 {total_liked}/{total_target}')
                like_log.append(build_log_entry(info, note_id, COVER_CFG, extra_fields={
                    '频道': ch_name, '序号': attempt,
                    '笔记作者_脱敏': mask_nickname(info.get('author')) if DO_MASK else '',
                    '笔记作者_哈希': anonymize_user_id(info.get('author', '')) if DO_ANONYMIZE else '',
                    '点赞数': after.get('count', ''),
                    '状态': '成功', '失败原因': ''
                }))
            else:
                print(f'  ⚠️ 点赞未生效（href: {after.get("href")}）')
                like_log.append(build_log_entry(info, note_id, COVER_CFG, extra_fields={
                    '频道': ch_name, '序号': attempt,
                    '笔记作者_脱敏': mask_nickname(info.get('author')) if DO_MASK else '',
                    '笔记作者_哈希': anonymize_user_id(info.get('author', '')) if DO_ANONYMIZE else '',
                    '点赞数': after.get('count', before.get('count', '')),
                    '状态': '失败', '失败原因': f'点赞未生效 href={after.get("href")}'
                }))
            save_log()

            # 关闭
            send_cmd('back', {})
            time.sleep(rand(LIKE_BACK_DELAY))

            # 间隔 / 休息
            if total_liked < total_target:
                if total_liked > 0 and total_liked % L['rest_every'] == 0 and ok:
                    rest = rand(L['rest_range'])
                    print(f'\n😴 已赞 {total_liked} 个，长休息 {rest:.0f}s...')
                    time.sleep(rest)
                else:
                    gap = rand(L['gap_range'])
                    print(f'  ⏸️ {gap:.0f}s...')
                    time.sleep(gap)

        print(f'\n📊 {ch_name} 完成: {ch_liked}/{ch_target}')

    # 保存日志
    print(f'\n{"="*50}')
    print(f'💾 完成: 新赞 {total_liked} 个 | 尝试 {attempt} 次 | 跳过 {len([r for r in like_log if r["状态"]=="跳过"])} 个已赞')
    # P1.15 改造：频道任务和搜索任务分别保存到独立文件，避免混淆
    if any(t['type'] == 'channel' for t in tasks):
        _save_to(_output_file)
        print(f'✅ 频道日志: {_output_file}')
    if any(t['type'] == 'search' for t in tasks):
        _save_to(_search_output_file)
        print(f'✅ 搜索日志: {_search_output_file}')


if __name__ == '__main__':
    main()

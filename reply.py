"""小红书拟人评论 - 配置驱动版
配置见 config.json 的 reply 节点。完整拟人流程：
刷 feed → 阅读笔记 → 翻评论 → 构思 → 逐字输入 → 回看 → 发送 → 停留。
每条回复间随机间隔，每 N 条长休息。
"""
import socket, urllib.request, urllib.parse, json, time, random, functools, os, sys
import pandas as pd
from exceptions import MCPDownError
from exception_map import EXCEPTION_MAP
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type
from cover_utils import load_cover_config, download_cover, cover_hyperlink, EXTRACT_INFO_JS, build_log_entry
from sort_utils import click_sort  # P1.30 排序工具（与 search.py 共用；同模块同 click_sort 协议）

print = functools.partial(print, flush=True)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

with open('config.json', 'r', encoding='utf-8') as f:
    CFG = json.load(f)

R = CFG['reply']
B = CFG['browse']
REPLY_BACK_DELAY = R.get('back_delay_range', [1.0, 2.5])
REPLY_POLL_INTERVAL = R.get('modal_poll_interval_range', [0.3, 1.0])
REPLY_POLL_MAX = R.get('modal_poll_max', 20)
# P1.17 改造：reply.py 同步支持封面下载（与 like.py 共用 cover_utils 模块）
COVER_CFG = load_cover_config(R)
# P1.27 目标作者评论模式：reply.owner_comment.enabled 时，按关键词搜索 → 判断目标作者 → 评论
OC = R.get('owner_comment') or {}
OC_ENABLED = bool(OC.get('enabled'))
SEARCH_KEYWORDS = OC.get('keywords', [])
SEARCH_LOAD_WAIT = R.get('search_load_wait', 6.0)
OWNER_MARKERS = OC.get('owner_markers') or ['分享', '记录', '打卡', '心得', '体验', '总结', '推荐']
DEALER_MARKERS = OC.get('dealer_markers') or ['广告', '推广', '优惠', '领取', '福利', '免费', '团购']
# P1.28 目标作者去重：已评论目标作者 user id 持久化文件（跨运行去重）
DONE_FILE = OC.get('done_file') or os.path.join('output', 'xhs-owner-comment-done.json')

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
# 与 collect.py / like.py 复用同一段 JS（与 EXCEPTION_MAP key 对齐）
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

def switch_channel(name):
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

def pick_note_and_click(exclude_ids, exclude_authors=()):
    """找 ≥min_likes 且 <max_likes 的可见卡片并点击标题链接"""
    min_likes = OC.get('min_likes', 1) if OC_ENABLED else CFG['min_likes']
    # 赞数上限：None=不限；赞<10 → max_likes=10（JS 端用 likes < maxLikes 判断，天然排除 10+）
    max_likes = OC.get('max_likes', None) if OC_ENABLED else None
    max_likes_js = 'null' if max_likes is None else int(max_likes)
    return send_cmd('execute_js', {
        'code': f'''
            (() => {{
                const exclude = new Set({json.dumps(exclude_ids)});
                const excludeAuthors = new Set({json.dumps(list(exclude_authors))});
                const minLikes = {min_likes};
                const maxLikes = {max_likes_js};
                if (document.querySelector('.note-detail-mask')) return {{ clicked: false, reason: 'modal open' }};
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
                for (const l of card.querySelectorAll('a[href*="/explore/"], a[href*="/search_result/"]')) {{
                        if (window.getComputedStyle(l).display !== 'none' && l.innerText.trim()) {{ link = l; break; }}
                    }}
                    if (!link) continue;
                    const rect = card.getBoundingClientRect();
                    if (rect.top < 0 || rect.top > window.innerHeight) continue;
                    const likeEl = card.querySelector('.like-wrapper .count') || card.querySelector('.like-wrapper');
                    const likes = parseCount(likeEl ? likeEl.innerText : '');
                    if (likes < minLikes) continue;
                    if (maxLikes !== null && likes >= maxLikes) continue;
                    const m = link.href.match(/(?:explore|search_result)\\/([a-f0-9]+)/);
                    if (m && exclude.has(m[1])) continue;
                    let authorId = '';
                    const profileLink = card.querySelector('a[href*="/user/profile/"]');
                    if (profileLink) {{
                        const am = (profileLink.getAttribute('href') || '').match(/\\/user\\/profile\\/([0-9a-f]+)/);
                        if (am) authorId = am[1];
                    }}
                    if (authorId && excludeAuthors.has(authorId)) continue;
                    candidates.push({{ link, likes, id: m ? m[1] : '', authorId, text: link.innerText.substring(0, 40) }});
                }}
                if (candidates.length > 0) {{
                    const pick = candidates[Math.floor(Math.random() * candidates.length)];
                    pick.link.click();
                    return {{ clicked: true, text: pick.text, id: pick.id, likes: pick.likes, authorId: pick.authorId || '' }};
                }}
                return {{ clicked: false, reason: 'no candidates' }};
            }})()
        '''
    }).get('result', {})

def _ensure_xhs_tab():
    """复用 search.py 标签页纪律：切回小红书标签页，避免 JS 在用户标签页执行。"""
    tabs = (send_cmd('list_tabs', {}, timeout=B['http_timeout']) or {}).get('result') or []
    for t in tabs:
        if 'xiaohongshu.com' in (t.get('url') or ''):
            send_cmd('switch_tab', {'tabId': t['id']}, timeout=B['http_timeout'])
            return t['id']
    return None


def exec_js(code):
    """包一层：拿 send_cmd 返回的 result 字段（dict / list / null），先 _ensure_xhs_tab。
    click_sort 等需要解包封装的模块依赖此函数。"""
    _ensure_xhs_tab()
    r = send_cmd('execute_js', {'code': code}, timeout=B['http_timeout'])
    return (r or {}).get('result')


def navigate_to_search(keyword):
    """导航到搜索结果页（复用当前小红书 tab，不新建）。"""
    _ensure_xhs_tab()
    url = f'https://www.xiaohongshu.com/search_result?keyword={urllib.parse.quote(keyword)}'
    send_cmd('execute_js', {'code': f'location.href = {json.dumps(url)}'}, timeout=B['http_timeout'])
    print(f'  🔎 搜索: {keyword}')
    time.sleep(SEARCH_LOAD_WAIT)
    # === 按 OC.sort_by 切排序（默认 comprehensive = 不点击） ===
    sort_by = OC.get('sort_by', 'comprehensive')
    try:
        res = click_sort(exec_js, sort_by=sort_by)
        if res.get('clicked'):
            print(f'  🔀 排序切换（{sort_by}）: {res}')
        elif res.get('error'):
            print(f'  ⚠️ 排序切换失败（sort_by={sort_by}）: {res["error"]}')
        # reason 分支（comprehensive / 不点击 chip）不打日志，避免噪音
    except MCPDownError as e:
        print(f'  ⚠️ 排序切换失败（sort_by={sort_by}）: {e}，按页面默认排序继续')
    for _ in range(random.randint(5, 8)):
        send_cmd('scroll', {'direction': 'down', 'pixels': random.randint(800, 1200)}, timeout=B['http_timeout'])
        time.sleep(random.uniform(2.5, 4.0))

def judge_owner():
    """提取弹窗正文并判断笔记作者是否像目标作者。返回 (is_owner, reason)。"""
    r = send_cmd('execute_js', {
        'code': '''
            (() => {
                const mask = document.querySelector('.note-detail-mask');
                if (!mask) return { error: 'no modal' };
                const scroller = mask.querySelector('.note-scroller');
                const text = scroller ? (scroller.innerText || '').slice(0, 1000) : '';
                const title = (mask.querySelector('.title, .note-title') || {}).innerText || '';
                return { title: (title || '').trim(), text: text.trim() };
            })()
        '''
    }).get('result') or {}
    if r.get('error'):
        return False, f'提取失败: {r["error"]}'
    content = f"{r.get('title', '')} {r.get('text', '')}"
    for m in DEALER_MARKERS:
        if m in content:
            return False, f'疑似经销商/广告（含「{m}」）'
    for m in OWNER_MARKERS:
        if m in content:
            return True, f'目标作者特征（含「{m}」）'
    return False, '无法确认目标作者身份（无明确目标作者特征）'

def load_done_authors():
    """读取已评论目标作者 user id 集合（跨运行去重，P1.28）。"""
    try:
        with open(DONE_FILE, 'r', encoding='utf-8') as f:
            return set(json.load(f).get('authors', []))
    except (FileNotFoundError, ValueError):
        return set()


def save_done_authors(authors):
    """持久化已评论目标作者 user id 集合（评论发出后立即写盘）。"""
    os.makedirs(os.path.dirname(os.path.abspath(DONE_FILE)) or '.', exist_ok=True)
    with open(DONE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'authors': sorted(authors),
                   'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')},
                  f, ensure_ascii=False, indent=2)


def clear_comment_input():
    """清空评论输入框残留：失败后防草稿残留/自动发送；弹窗不在时自动 no-op。
    用 createRange+selectNodeContents 而非 selectAll（后者在 contenteditable 上不可靠）。"""
    send_cmd('execute_js', {'code': '''
        (() => {
            const i = document.querySelector('.note-detail-mask .engage-bar .input-box .content-edit p.content-input');
            if (!i) return false;
            i.focus();
            const range = document.createRange();
            range.selectNodeContents(i);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            document.execCommand('delete');
            return true;
        })()
    '''})


def mark_author_attempted(done_authors, author_key):
    """评论已尝试（内容可能已注入输入框/已发出）→ 目标作者去重，防下一轮重复评论。"""
    if OC_ENABLED and author_key:
        done_authors.add(author_key)
        save_done_authors(done_authors)


def fail_attempt(done_authors, reason, info, note_id, comment_text, cur, author_key, mark=True):
    """失败统一出口：记日志 → 落盘 → 去重 → 清残留输入 → 弹窗活着才 back（防 §1.0 链式 back）。"""
    reply_log.append(build_log_entry(info, note_id, COVER_CFG, send_cmd=send_cmd, extra_fields={
        '序号': cur, '评论内容': comment_text, '状态': '失败', '失败原因': reason
    }))
    save_log()
    if mark:
        mark_author_attempted(done_authors, author_key)
    try:
        clear_comment_input()
        if is_modal_open():
            send_cmd('back', {})
            time.sleep(rand(REPLY_BACK_DELAY))
    except MCPDownError:
        print('  ⚠️ 失败清理未完成（MCP 抖动），由下一轮开头 URL 守卫兜底')
    used_ids.append(note_id)


def type_like_human(text):
    """逐字输入，模拟人类打字节奏。返回 (ok, injected_count)：
      ok=True   → 全部字符已注入
      ok=False  → 任一字符失败，injected_count 是失败前已注入的字符数（驱动 done_authors 是否 mark）
    """
    # 预检输入框存在（一次性，避免循环里重复查不到 false 返回）
    pre = send_cmd('execute_js', {'code': '!!document.querySelector(".note-detail-mask .engage-bar .input-box .content-edit p.content-input")'})
    if not pre.get('result'):
        return False, 0
    injected = 0
    for char in text:
        r = send_cmd('execute_js', {
            'code': f'''
                (() => {{
                    const input = document.querySelector('.note-detail-mask .engage-bar .input-box .content-edit p.content-input');
                    if (!input) return false;
                    input.focus();
                    document.execCommand('insertText', false, {json.dumps(char)});
                    return true;
                }})()
            '''
        })
        if not r.get('result'):
            return False, injected
        injected += 1
        if random.random() < R['type_pause_prob']:
            time.sleep(rand(R['type_pause_range']))
        else:
            time.sleep(rand(R['type_char_range']))
    return True, injected

reply_log = []
used_ids = []

# 输出文件名加时间戳，防止覆盖
import datetime
_ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
_output_file = R['output_file'].replace('.xlsx', f'-{_ts}.xlsx')

def save_log():
    """增量保存日志，每次操作后调用。"""
    if reply_log:
        pd.DataFrame(reply_log).to_excel(_output_file, index=False)

def main():
    if OC_ENABLED:
        target_count = OC.get('count', R['count'])
        comment_texts = OC.get('comment_texts') or R['comment_texts']
    else:
        target_count = R['count']
        comment_texts = R['comment_texts']
    if OC_ENABLED and SEARCH_KEYWORDS:
        navigate_to_search(SEARCH_KEYWORDS[0])
    done_authors = load_done_authors() if OC_ENABLED else set()
    if OC_ENABLED:
        print(f'  📋 已评论目标作者 {len(done_authors)} 位，自动跳过（{DONE_FILE}）')
    idx = 0
    tries = 0
    max_tries = OC.get('max_tries', max(target_count * 20, 20)) if OC_ENABLED else target_count
    while idx < target_count:
        tries += 1
        if tries > max_tries:
            print(f'  ⛔ 已达最大尝试 {max_tries} 次，停止')
            break
        cur = idx + 1
        print(f'\n{"="*50}')
        print(f'💬 回复 {cur}/{target_count}')
        print(f'{"="*50}')

        # === URL 守卫：检测到页面偏离搜索页时直接 navigate 回搜索页（不再 back，避免链式跳转） ===
        try:
            cur_url = send_cmd('execute_js', {'code': 'location.href'}, timeout=B['http_timeout']).get('result', '') or ''
            if OC_ENABLED and SEARCH_KEYWORDS and '/search_result' not in cur_url:
                print(f'  ⚠️ 页面偏离搜索页（{cur_url[:60]}），强制 navigate 回搜索页')
                navigate_to_search(SEARCH_KEYWORDS[0])
                continue    # 本轮不再操作，留给下一轮选卡
        except MCPDownError:
            pass

        if is_modal_open():
            send_cmd('back', {})
            time.sleep(rand(REPLY_BACK_DELAY))

        # 拟人刷 feed
        for i in range(random.randint(*B['scrolls_per_attempt'])):
            send_cmd('scroll', {'direction': 'down', 'pixels': random.randint(*B['scroll_px_range'])})
            time.sleep(rand(B['scroll_pause_range']))

        # 找笔记点击（含重试滚动）
        cr = {}
        for attempt in range(CFG['max_scroll_attempts']):
            cr = pick_note_and_click(used_ids, done_authors)
            if cr.get('clicked'):
                break
            send_cmd('scroll', {'direction': 'down', 'pixels': random.randint(*B['scroll_px_range'])})
            time.sleep(rand(B['scroll_pause_range']))
        if not cr.get('clicked'):
            print('  ❌ 找不到笔记，跳过')
            reply_log.append({'序号': cur, '封面图片': '', '封面图片_本地': '', '状态': '失败', '失败原因': '找不到符合条件的笔记'})
            save_log()
            idx += 1
            continue

        note_id = cr.get('id', '')
        note_author_id = cr.get('authorId') or ''
        print(f'  👆 {cr.get("text")}（❤️{cr.get("likes")}）')

        # 等弹窗
        for _ in range(REPLY_POLL_MAX):
            if is_modal_open():
                break
            time.sleep(rand(REPLY_POLL_INTERVAL))
        else:
            print('  ❌ 弹窗未打开')
            reply_log.append({'序号': cur, '笔记ID': note_id, '封面图片': '', '封面图片_本地': '', '状态': '失败', '失败原因': '弹窗未打开'})
            save_log()
            used_ids.append(note_id)
            idx += 1
            continue

        # 异常分级探测：CAPTCHA → 整个 reply 任务终止；NOTE_GONE → 跳过本笔记
        # 借鉴 MediaCrawler media_platform/xhs/exception.py
        exception_class = detect_exception_class()
        if exception_class == 'CAPTCHA':
            label, action = EXCEPTION_MAP['CAPTCHA']
            print(f'  ⛔ {label}（{action}）')
            reply_log.append({'序号': cur, '笔记ID': note_id, '封面图片': '', '封面图片_本地': '', '状态': '失败', '失败原因': label})
            save_log()
            break  # 整个 reply 任务终止
        elif exception_class == 'NOTE_GONE':
            label, action = EXCEPTION_MAP['NOTE_GONE']
            print(f'  ⏭️ {label}（{action}）')
            reply_log.append({'序号': cur, '笔记ID': note_id, '状态': '失败', '失败原因': label})
            save_log()
            used_ids.append(note_id)
            idx += 1
            continue
        elif exception_class == 'MCP_DOWN':
            label, action = EXCEPTION_MAP[exception_class]
            print(f'  🚫 {label}（{action}）')
            reply_log.append({'序号': cur, '笔记ID': note_id, '封面图片': '', '封面图片_本地': '', '状态': '失败', '失败原因': label})
            save_log()
            used_ids.append(note_id)
            idx += 1
            continue

        # 笔记信息
        info = send_cmd('execute_js', {'code': EXTRACT_INFO_JS}).get('result', {})
        # P1.21: info JS 异常不再静默（gpt-5.6-sol finding #5）
        if info.get('error'):
            print(f'  ⚠️ info JS 异常: {info["error"]}')
        print(f'  笔记: {info.get("author")} | {info.get("title", "")[:25]}')

        # P1.28 目标作者去重：该作者已被评论过 → 跳过（卡片级已挡，弹窗级兜底）
        author_key = note_author_id or (info.get('author_id') or '')
        if OC_ENABLED and author_key and author_key in done_authors:
            print(f'  ⏭️ 跳过（该目标作者已评论过）: {info.get("author")} | {info.get("title", "")[:25]}')
            reply_log.append(build_log_entry(info, note_id, COVER_CFG, send_cmd=send_cmd, extra_fields={
                '序号': cur, '评论内容': '', '状态': '跳过', '失败原因': f'该目标作者已评论过（去重 author={author_key}）'
            }))
            save_log()
            # §1.0：弹窗活着才 back，防链式触发 SPA 跳变
            try:
                if is_modal_open():
                    send_cmd('back', {})
                    time.sleep(rand(REPLY_BACK_DELAY))
            except MCPDownError:
                pass
            used_ids.append(note_id)
            continue

        # P1.27 目标作者判断（目标作者评论模式）：非目标作者跳过，避免评论给经销商/广告号
        # require_owner=false 时跳过目标作者判断（所有笔记都评论，无目标作者限制）
        if OC_ENABLED and OC.get('require_owner', True):
            is_owner, reason = judge_owner()
            if not is_owner:
                print(f'  ⏭️ 跳过（{reason}）: {info.get("author")} | {info.get("title", "")[:25]}')
                reply_log.append(build_log_entry(info, note_id, COVER_CFG, send_cmd=send_cmd, extra_fields={
                    '序号': cur, '评论内容': '', '状态': '跳过', '失败原因': f'非目标作者: {reason}'
                }))
                save_log()
                # §1.0：弹窗活着才 back
                try:
                    if is_modal_open():
                        send_cmd('back', {})
                        time.sleep(rand(REPLY_BACK_DELAY))
                except MCPDownError:
                    pass
                used_ids.append(note_id)
                continue
            print(f'  ✅ 目标作者确认: {reason}')

        # 拟人：看笔记 + 翻评论
        read_time = rand(B['read_time_range'])
        print(f'  📖 看笔记 {read_time:.1f}s...')
        time.sleep(read_time)
        for i in range(random.randint(2, 3)):
            send_cmd('execute_js', {'code': '(() => { const s = document.querySelector(".note-scroller"); if (s) s.scrollTop += 300; })()'})
            time.sleep(rand(B['scroll_pause_range']))

        # 点输入框 + 构思
        comment_text = random.choice(comment_texts)
        send_cmd('execute_js', {
            'code': '(() => { const i = document.querySelector(".note-detail-mask .engage-bar .input-box .content-edit p.content-input"); if (i) { i.focus(); i.click(); } })()'
        })
        think_time = rand(R['think_range'])
        print(f'  🤔 构思 {think_time:.1f}s...')
        time.sleep(think_time)

        # 逐字输入
        print(f'  ⌨️ 输入「{comment_text}」...')
        ok, injected = type_like_human(comment_text)
        if not ok:
            print(f'  ❌ 输入失败（已注入 {injected}/{len(comment_text)} 字符）')
            # 只在「已注入字符 > 0」时 mark，避免弹窗未开等环境性失败永久污染目标作者池
            fail_attempt(done_authors, f'输入失败(已注入{injected}/{len(comment_text)})',
                         info, note_id, comment_text, cur, author_key, mark=injected > 0)
            idx += 1
            continue

        # 校验 + 回看
        check = send_cmd('execute_js', {
            'code': '(() => { const i = document.querySelector(".note-detail-mask .engage-bar .input-box .content-edit p.content-input"); return i ? i.innerText.trim() : ""; })()'
        }).get('result', '')
        if comment_text not in check:
            print(f'  ❌ 输入校验失败（框内: 「{check}」）')
            fail_attempt(done_authors, f'输入校验失败:{check}',
                         info, note_id, comment_text, cur, author_key)
            idx += 1
            continue
        time.sleep(rand(R['review_range']))

        # 发送
        send_result = send_cmd('execute_js', {
            'code': '''
                (() => {
                    const mask = document.querySelector('.note-detail-mask');
                    for (const b of mask.querySelectorAll('.engage-bar button')) {
                        if (b.innerText.trim() === '发送') { b.click(); return { clicked: true }; }
                    }
                    return { clicked: false };
                })()
            '''
        }).get('result', {})
        if not send_result.get('clicked'):
            print('  ❌ 发送按钮未找到')
            fail_attempt(done_authors, '发送按钮未找到',
                         info, note_id, comment_text, cur, author_key)
            idx += 1
            continue

        # P1.17 改造：发送按钮已点，顺手下载封面到本地（无论后续验证是否成功）
        # 使用 build_log_entry 在验证前下载封面
        local_cover = ''
        if info.get('cover') and note_id:
            local_cover = download_cover(info['cover'], note_id, COVER_CFG)
            if local_cover:
                print(f'  📥 封面已下载: {local_cover}')

        # 停留 + 验证
        time.sleep(rand(R['after_send_range']))
        verify = send_cmd('execute_js', {
            'code': f'''
                (() => {{
                    const mask = document.querySelector('.note-detail-mask');
                    if (!mask) return {{ modalClosed: true }};
                    for (const item of mask.querySelectorAll('.comments-container .parent-comment')) {{
                        const c = item.querySelector('.content .note-text') || item.querySelector('.note-text');
                        if (c && c.innerText.trim() === {json.dumps(comment_text)}) return {{ found: true }};
                    }}
                    return {{ found: false }};
                }})()
            '''
        }).get('result', {})

        ok = bool(verify.get('found'))
        print(f'  {"🎉 成功" if ok else "⚠️ 未验证到"}')
        # 使用 build_log_entry，传入已下载的 local_cover 避免重复下载
        reply_log.append(build_log_entry(info, note_id, COVER_CFG, send_cmd=send_cmd, local_cover=local_cover, extra_fields={
            '序号': cur,
            '评论内容': comment_text,
            '状态': '成功' if ok else '待确认',
            '失败原因': '' if ok else '评论区未找到'
        }))
        save_log()
        used_ids.append(note_id)

        # P1.28 目标作者去重：评论已发出（成功/待确认都算），持久化该作者
        mark_author_attempted(done_authors, author_key)

        # 关闭弹窗回 feed
        send_cmd('back', {})
        time.sleep(rand(REPLY_BACK_DELAY))

        # 拟人间隔 / 长休息
        idx += 1
        if idx < target_count:
            if idx % R['rest_every'] == 0:
                rest = rand(R['rest_range'])
                print(f'\n😴 已回复 {idx} 条，长休息 {rest:.0f}s...')
                time.sleep(rest)
            else:
                gap = rand(R['interval_range'])
                print(f'  ⏸️ 间隔 {gap:.0f}s...')
                time.sleep(gap)

    # 保存日志
    print(f'\n{"="*50}')
    ok_count = len([r for r in reply_log if r['状态'] == '成功'])
    print(f'💾 回复完成: 成功 {ok_count}/{len(reply_log)}')
    if reply_log:
        save_log()
        print(f'✅ 日志已保存: {_output_file}')
        for row in reply_log:
            mark = '✅' if row['状态'] == '成功' else '❌'
            print(f'  {mark} [{row["序号"]}] {row.get("笔记作者", "")} | {str(row.get("笔记标题", ""))[:20]} | {row.get("评论内容", "")}')


if __name__ == '__main__':
    main()

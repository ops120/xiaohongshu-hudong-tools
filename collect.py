"""小红书笔记/评论采集 - 配置驱动版（多频道）
配置见 config.json。支持按频道采集，自动点击频道 tab。
"""
import socket, urllib.request, json, time, random, functools, os, sys, datetime
import pandas as pd
from privacy import mask_nickname, anonymize_user_id
from exceptions import MCPDownError, XhsException
from exception_map import EXCEPTION_MAP
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

print = functools.partial(print, flush=True)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

with open('config.json', 'r', encoding='utf-8') as f:
    CFG = json.load(f)

COLLECT = CFG.get('collect', {})
HOT_MODE = COLLECT.get('hot_mode', False)
HOT_COMMENT_SCROLL_COUNT = COLLECT.get('hot_comment_scroll_count', 6)
PRIVACY = CFG.get('privacy', {})
DO_MASK = PRIVACY.get('mask_nickname', False)
DO_ANONYMIZE = PRIVACY.get('anonymize_user_id', False)

# === 任务 #9 P1 借鉴项：评论子楼层抓取 ===
# 借鉴 MediaCrawler media_platform/xhs/client.py:375-405 get_note_sub_comments()
# 默认关闭：开启后每篇笔记多 3-5s（点击展开按钮 + 等异步 + 多次滚动）
GET_SUB = COLLECT.get('get_sub_comments', False)
EXPAND_REPLIES_WAIT_RANGE = COLLECT.get('expand_replies_wait_range', [1.0, 2.0])
SUB_COMMENT_EXTRACT_PASSES = COLLECT.get('sub_comment_extract_passes', 2)

B = CFG['browse']

def rand(r):
    return random.uniform(r[0], r[1])

@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type(MCPDownError),
    reraise=True,
)
def send_cmd(action, params, timeout=None):
    """调 BrowserMCP；MCP 抖动（URLError/ConnectionResetError/timeout）时自动重试 3 次。

    timeout: None 时走 B['http_timeout']（默认 60s，IIFE 可能要 30+ 秒）。
    重试只针对 MCPDownError（瞬时抖动）；JSONDecodeError / KeyError 等代码错误不重试。
    """
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


# 评论区批量滚动 + 单次回传 IIFE（借鉴 MediaCrawler 一次循环拉到 max_count 的思路）
# - 浏览器内 6 轮循环：scroller.scrollTop += 200~500 + sleep 1500~3000ms + 增量提取
# - Map<key, comment> 去重，key = dataset.id || outerHTML.slice(0, 200)
# - 到底检测：scrollTop + clientHeight >= scrollHeight - 100 则 break
# - 返回 author/title/content/互动数 + comments 数组 + history.back()
# 单次 send_cmd 即可拿到所有数据，把 5-8 次往返压缩到 1 次
BATCH_EXTRACT_JS = '''
    (async () => {
        const mask = document.querySelector('.note-detail-mask');
        if (!mask) return { error: 'no modal' };
        const scroller = mask.querySelector('.note-scroller');
        if (!scroller) return { error: 'no scroller' };

        const num = (s) => {
            s = (s || '').trim();
            if (!s) return 0;
            if (s.includes('万')) return Math.round(parseFloat(s) * 10000);
            const m = s.match(/\\d+/);
            return m ? parseInt(m[0]) : 0;
        };

        // === 增量滚动 + 提取评论（去重 Map）===
        const all = new Map();
        for (let r = 0; r < {max_scroll_rounds}; r++) {
            scroller.scrollTop += 200 + Math.random() * 300;
            await new Promise(res => setTimeout(res, 1500 + Math.random() * 1500));
            const items = mask.querySelectorAll('.comments-container .parent-comment');
            for (const item of items) {
                const key = item.dataset.id || item.outerHTML.slice(0, 200);
                if (all.has(key)) continue;
                const a = item.querySelector('.author');
                const c = item.querySelector('.content .note-text') || item.querySelector('.note-text');
                const user = a ? a.innerText.trim().split('\\n')[0].trim() : '';
                const text = c ? c.innerText.trim() : '';
                if (!user || !text) continue;
                const likes = num(item.querySelector('.info .interactions .like .count')?.innerText || '0');
                const replies = num(item.querySelector('.info .interactions .reply .count')?.innerText || '0');
                all.set(key, { user, text, likes, replies });
            }
            if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 100) break;
        }

        // === 笔记元数据 ===
        const q = (sel) => mask.querySelector(sel);
        const txt = (el) => el ? el.innerText.trim() : '';
        const author = txt(q('.author-container .username')) || txt(q('.author-container .name'));
        const title = txt(q('.note-content .title'));
        const content = txt(q('.note-content .desc'));
        const likeCount = num(txt(q('.engage-bar .like-wrapper .count')));
        const collectCount = num(txt(q('.engage-bar .collect-wrapper .count')));
        const commentCount = num(txt(q('.engage-bar .chat-wrapper .count')));

        history.back();
        return {
            author, title, content, url: location.href,
            likeCount, collectCount, commentCount,
            comments: Array.from(all.values()),
        };
    })()
'''

# EXTRACT_JS = BATCH_EXTRACT_JS 的扩展版（任务 #5：增加 imageList 图片列表提取）
# - 不替换 BATCH_EXTRACT_JS（兼容老调用方）
# - 主流程通过 extract_note() 间接调用此常量
# - 用 | 做分隔符避免与 URL 中的逗号冲突
EXTRACT_JS = '''
    (async () => {
        const mask = document.querySelector('.note-detail-mask');
        if (!mask) return { error: 'no modal' };
        const scroller = mask.querySelector('.note-scroller');
        if (!scroller) return { error: 'no scroller' };

        const num = (s) => {
            s = (s || '').trim();
            if (!s) return 0;
            if (s.includes('万')) return Math.round(parseFloat(s) * 10000);
            const m = s.match(/\\d+/);
            return m ? parseInt(m[0]) : 0;
        };

        // === 增量滚动 + 提取评论（去重 Map）===
        const all = new Map();
        for (let r = 0; r < {max_scroll_rounds}; r++) {
            scroller.scrollTop += 200 + Math.random() * 300;
            await new Promise(res => setTimeout(res, 1500 + Math.random() * 1500));
            const items = mask.querySelectorAll('.comments-container .parent-comment');
            for (const item of items) {
                const key = item.dataset.id || item.outerHTML.slice(0, 200);
                if (all.has(key)) continue;
                const a = item.querySelector('.author');
                const c = item.querySelector('.content .note-text') || item.querySelector('.note-text');
                const user = a ? a.innerText.trim().split('\\n')[0].trim() : '';
                const text = c ? c.innerText.trim() : '';
                if (!user || !text) continue;
                const likes = num(item.querySelector('.info .interactions .like .count')?.innerText || '0');
                const replies = num(item.querySelector('.info .interactions .reply .count')?.innerText || '0');
                all.set(key, { user, text, likes, replies });
            }
            if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 100) break;
        }

        // === 笔记元数据 ===
        const q = (sel) => mask.querySelector(sel);
        const txt = (el) => el ? el.innerText.trim() : '';
        const author = txt(q('.author-container .username')) || txt(q('.author-container .name'));
        const title = txt(q('.note-content .title'));
        const content = txt(q('.note-content .desc'));
        const likeCount = num(txt(q('.engage-bar .like-wrapper .count')));
        const collectCount = num(txt(q('.engage-bar .collect-wrapper .count')));
        const commentCount = num(txt(q('.engage-bar .chat-wrapper .count')));

        // === 图片列表（任务 #5，借鉴 MediaCrawler store/xhs/_store_impl.py:136-211 image_list）===
        const imgs = Array.from(mask.querySelectorAll('.note-content img, .swiper-slide img'))
            .map(img => img.src || img.getAttribute('data-src'))
            .filter(Boolean)
            .filter(src => !src.startsWith('data:'));  // 排除 data:image/svg+xml;base64 占位图
        const imageList = imgs.join('|');

        history.back();
        return {
            author, title, content, url: location.href,
            likeCount, collectCount, commentCount,
            imageList,
            comments: Array.from(all.values()),
        };
    })()
'''

# EXTRACT_FROM_STATE_JS（任务 #2：INITIAL_STATE 兜底）
# 借鉴 MediaCrawler media_platform/xhs/extractor.py noteDetailMap[noteId].note
# - 当 DOM 选择器失效（小红书改版 CSS class 哈希）时救命
# - 用 .format(note_id=note_id) 注入，JS 中所有花括号用 {{ / }} 转义
# - 注意：INITIAL_STATE 不一定有完整的互动数和评论，DOM 优先
EXTRACT_FROM_STATE_JS = '''
    (() => {{
        const state = window.__INITIAL_STATE__ || {{}};
        // 路径降级：实测 2026-08 真实路径是 state.note.noteDetailMap（不是 state.noteDetail）
        // 保留旧路径以防小红书回退
        const noteDetailMap = state.note?.noteDetailMap
            || state.noteDetail
            || state.noteDetailMap
            || {{}};
        // 实测：navigate 到无 token 链接时，mapKeys = ['undefined']（不是真实 note_id）
        // 真实 note_id 查不到时，强制 fallback 到 map 第一项
        let entry = noteDetailMap["{note_id}"];
        if (!entry || (entry.note && !entry.note.title && !entry.note.desc)) {{
            const firstVal = Object.values(noteDetailMap)[0];
            if (firstVal && firstVal.note && (firstVal.note.title || firstVal.note.desc)) {{
                entry = firstVal;
            }}
        }}
        if (!entry) return {{ error: 'no state' }};
        const note = entry.note || entry;
        // 互动数字段：实测是字符串（\"1140\"）而非数字，统一 parseInt
        const num = (s) => {{ const m = String(s || '').match(/\\d+/); return m ? parseInt(m[0]) : 0; }};
        // 图片列表：note.imageList[].infoList[].url 取 WB_DFT 场景的 url
        const imageList = (note.imageList || [])
            .map(img => {{
                const list = img.infoList || [];
                const dft = list.find(i => i.imageScene === 'WB_DFT') || list[0];
                return dft ? dft.url : '';
            }})
            .filter(Boolean)
            .join('|');
        return {{
            author: (note.user?.nickname) || '',
            title: note.title || (note.desc || '').slice(0, 255),
            content: note.desc || '',
            url: location.href,
            likeCount: num(note.interactInfo?.likedCount),
            collectCount: num(note.interactInfo?.collectedCount),
            commentCount: num(note.interactInfo?.commentCount),
            imageList,
            comments: [],
        }};
    }})()
'''


# EXTRACT_WITH_SUB_JS（任务 #9 P1 借鉴项：评论子楼层抓取）
# 借鉴 MediaCrawler media_platform/xhs/client.py:375-405 get_note_sub_comments()
# - 在 EXTRACT_JS 基础上 + 点击「展开回复」按钮 + 多次滚动收集子评论
# - 用 .format() 注入 expand_replies_wait_ms（按钮点击后等异步展开）和 sub_comment_extract_passes（多次滚动次数）
# - 不修改 EXTRACT_JS / EXTRACT_FROM_STATE_JS 的格式化机制
# - 与 EXTRACT_JS 共存；extract_note() 根据 GET_SUB 选择调用
# - 返回 dict 增 subComments / subCommentCount 两个字段
# - 注意：JS 字符串里没有字面量 {{ }}（对象字面量），所以 .format() 安全
EXTRACT_WITH_SUB_JS = '''
    (async () => {
        const mask = document.querySelector('.note-detail-mask');
        if (!mask) return { error: 'no modal' };
        const scroller = mask.querySelector('.note-scroller');
        if (!scroller) return { error: 'no scroller' };

        const num = (s) => {
            s = (s || '').trim();
            if (!s) return 0;
            if (s.includes('万')) return Math.round(parseFloat(s) * 10000);
            const m = s.match(/\\d+/);
            return m ? parseInt(m[0]) : 0;
        };

        // === 增量滚动 + 提取顶层评论（去重 Map）===
        const all = new Map();
        for (let r = 0; r < {max_scroll_rounds}; r++) {
            scroller.scrollTop += 200 + Math.random() * 300;
            await new Promise(res => setTimeout(res, 1500 + Math.random() * 1500));
            const items = mask.querySelectorAll('.comments-container .parent-comment');
            for (const item of items) {
                const key = item.dataset.id || item.outerHTML.slice(0, 200);
                if (all.has(key)) continue;
                const a = item.querySelector('.author');
                const c = item.querySelector('.content .note-text') || item.querySelector('.note-text');
                const user = a ? a.innerText.trim().split('\\n')[0].trim() : '';
                const text = c ? c.innerText.trim() : '';
                if (!user || !text) continue;
                const likes = num(item.querySelector('.info .interactions .like .count')?.innerText || '0');
                const replies = num(item.querySelector('.info .interactions .reply .count')?.innerText || '0');
                all.set(key, { user, text, likes, replies });
            }
            if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 100) break;
        }

        // === #9 子评论提取 ===
        // 1. 点击「展开回复」按钮（多个 selector 兜底 + try-catch 避免虚拟列表移除抛错）
        const expandBtns = mask.querySelectorAll('[class*="expand-replies"], [class*="show-sub"], [class*="show-more-replies"]');
        for (const btn of expandBtns) {
            try { btn.click(); } catch (e) {}
        }
        // 2. 等异步展开
        await new Promise(r => setTimeout(r, {expand_replies_wait_ms}));

        // 3. 多次滚动收集子评论（虚拟列表完全渲染；dedup 走 parent + user + text 首 50 字符）
        const subMap = new Map();
        for (let pass = 0; pass < {sub_comment_extract_passes}; pass++) {
            scroller.scrollTop += 300 + Math.random() * 200;
            await new Promise(res => setTimeout(res, 1500));
            const subs = mask.querySelectorAll('.sub-comment, .child-comment, [class*="sub-comment"], [class*="child-comment"]');
            for (const sub of subs) {
                const a = sub.querySelector('.author');
                const c = sub.querySelector('.content .note-text') || sub.querySelector('.note-text');
                const user = a ? a.innerText.trim().split('\\n')[0].trim() : '';
                const text = c ? c.innerText.trim() : '';
                const parentId = sub.dataset.parentCommentId || sub.closest('.parent-comment')?.dataset.id || '';
                if (!user || !text) continue;
                const key = parentId + '||' + user + '||' + text.slice(0, 50);
                if (subMap.has(key)) continue;
                subMap.set(key, { user, text, parentId });
            }
        }
        const subComments = Array.from(subMap.values());
        const subCommentCount = subComments.length;

        // === 笔记元数据 ===
        const q = (sel) => mask.querySelector(sel);
        const txt = (el) => el ? el.innerText.trim() : '';
        const author = txt(q('.author-container .username')) || txt(q('.author-container .name'));
        const title = txt(q('.note-content .title'));
        const content = txt(q('.note-content .desc'));
        const likeCount = num(txt(q('.engage-bar .like-wrapper .count')));
        const collectCount = num(txt(q('.engage-bar .collect-wrapper .count')));
        const commentCount = num(txt(q('.engage-bar .chat-wrapper .count')));

        // === 图片列表（任务 #5）===
        const imgs = Array.from(mask.querySelectorAll('.note-content img, .swiper-slide img'))
            .map(img => img.src || img.getAttribute('data-src'))
            .filter(Boolean)
            .filter(src => !src.startsWith('data:'));  // 排除 data:image/svg+xml;base64 占位图
        const imageList = imgs.join('|');

        history.back();
        return {
            author, title, content, url: location.href,
            likeCount, collectCount, commentCount,
            imageList,
            comments: Array.from(all.values()),
            subComments,
            subCommentCount,
        };
    })()
'''


def extract_note(note_id, max_comments=None):
    """先 DOM 提取（EXTRACT_JS / EXTRACT_WITH_SUB_JS），失败 fallback 到 window.__INITIAL_STATE__。

    任务 #2 P1 借鉴项：INITIAL_STATE 兜底。DOM 改版 CSS class 哈希时的救命稻草。
    任务 #9 P1 借鉴项：根据 collect.get_sub_comments 开关决定是否走 EXTRACT_WITH_SUB_JS（抓子评论）。
    note_id: 笔记 ID（用于 INITIAL_STATE 兜底时查 noteDetailMap）
    max_comments: 预留参数（当前未使用，评论截取在 Python 端按 hot/normal 模式分别处理）

    返回：
      GET_SUB=False 成功 → dict（{author, title, content, url, likeCount, collectCount, commentCount, imageList, comments}）
      GET_SUB=True  成功 → dict（多了 subComments / subCommentCount 两个字段）
      失败 → {'error': 'both DOM and INITIAL_STATE failed', 'comments': [], 'imageList': ''}（GET_SUB 时也会带 'subComments': [], 'subCommentCount': 0）

    防御性读 GET_SUB 等全局变量：测试场景（test_p1_collect.py 用 AST 切片）下 globals 不含这些值，
    自动 fallback 到默认（GET_SUB=False），保证向后兼容。生产环境 globals 含这些值（来自模块顶部 COLLECT.get(...)）。
    """
    # === 任务 #9 P1：防御性读 GET_SUB 配置（兼容测试场景下未注入 globals） ===
    _get_sub = globals().get('GET_SUB', False)
    _wait_range = globals().get('EXPAND_REPLIES_WAIT_RANGE', [1.0, 2.0])
    _passes = globals().get('SUB_COMMENT_EXTRACT_PASSES', 2)

    _scroll_rounds = globals().get('HOT_COMMENT_SCROLL_COUNT', 6)

    if _get_sub:
        # 用 .replace() 注入 wait_ms / passes（不用 .format()，因为 EXTRACT_WITH_SUB_JS 含 JS 字面量 {}）
        # 默认值取 EXPAND_REPLIES_WAIT_RANGE 上限（保守等更久）
        wait_ms = int(_wait_range[1] * 1000)
        dom_code = (
            EXTRACT_WITH_SUB_JS
            .replace('{expand_replies_wait_ms}', str(wait_ms))
            .replace('{sub_comment_extract_passes}', str(_passes))
            .replace('{max_scroll_rounds}', str(_scroll_rounds))
        )
        dom_timeout = 90  # 比 EXTRACT_JS 多 ~30s 覆盖子评论抓取
    else:
        dom_code = EXTRACT_JS.replace('{max_scroll_rounds}', str(_scroll_rounds))
        dom_timeout = 60

    # 1. DOM 路径
    try:
        data = send_cmd('execute_js', {'code': dom_code}, timeout=dom_timeout)
    except MCPDownError as e:
        print(f'    ⚠️ DOM 提取 MCP 故障: {e}，尝试 INITIAL_STATE 兜底...')
        data = None
    note_data = (data or {}).get('result') or {}
    if note_data and 'error' not in note_data:
        # GET_SUB 时确保下游有 subComments / subCommentCount（EXTRACT_WITH_SUB_JS 总会带，但兜一手）
        if _get_sub:
            note_data.setdefault('subComments', [])
            note_data.setdefault('subCommentCount', 0)
        return note_data

    if data is not None:
        print('    ⚠️ DOM 提取失败，尝试 INITIAL_STATE 兜底...')

    # 2. INITIAL_STATE 兜底路径（无子评论，固定为 [] / 0）
    try:
        state_data = send_cmd('execute_js', {
            'code': EXTRACT_FROM_STATE_JS.format(note_id=note_id)
        })
    except MCPDownError as e:
        print(f'    ⚠️ INITIAL_STATE 提取 MCP 故障: {e}')
        err = {'error': 'both DOM and INITIAL_STATE failed', 'comments': [], 'imageList': ''}
        if _get_sub:
            err['subComments'] = []
            err['subCommentCount'] = 0
        return err

    state_result = (state_data or {}).get('result') or {}
    if state_result and 'error' not in state_result:
        print('    ✅ INITIAL_STATE 兜底成功')
        if _get_sub:
            state_result.setdefault('subComments', [])
            state_result.setdefault('subCommentCount', 0)
        return state_result

    # 3. 两次都失败
    err = {'error': 'both DOM and INITIAL_STATE failed', 'comments': [], 'imageList': ''}
    if _get_sub:
        err['subComments'] = []
        err['subCommentCount'] = 0
    return err


# 浏览器端异常探测：返回 'CAPTCHA' / 'NOTE_GONE' / null
# 借鉴 MediaCrawler client.py:115-154 状态码映射；前端能拿到的两类先在前端探测。
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
    网络异常（IP_BLOCKED / MCP_DOWN）由 send_cmd 层识别，不在这里探测。
    返回字符串键与 EXCEPTION_MAP 一致。
    """
    try:
        r = send_cmd('execute_js', {'code': DETECT_CLASS_JS})
    except MCPDownError as e:
        # 探测本身就掉线 = MCP 整体不可用
        print(f'  ⚠️ detect 调用失败：{e}')
        return 'MCP_DOWN'
    return (r or {}).get('result')


def is_modal_open():
    try:
        r = send_cmd('execute_js', {
            'code': '(() => { const m = document.querySelector(".note-detail-mask"); if (!m) return false; const r = m.getBoundingClientRect(); return r.width > 300 && r.height > 300; })()'
        })
        return bool(r.get('result'))
    except (MCPDownError, json.JSONDecodeError):
        return False

def switch_channel(name):
    """点击频道 tab，返回是否成功"""
    safe_name = json.dumps(name)
    r = send_cmd('execute_js', {
        'code': f'''
            (() => {{
                const tabs = document.querySelectorAll('[class*="channel-content"], [class*="channel-item"]');
                for (const tab of tabs) {{
                    if (tab.innerText.trim() === {safe_name}) {{
                        tab.click();
                        return true;
                    }}
                }}
                // 也可能在更多频道里
                const moreTabs = document.querySelectorAll('a, span, div');
                for (const t of moreTabs) {{
                    if (t.innerText.trim() === {safe_name} && t.closest('[class*="channel"]')) {{
                        t.click();
                        return true;
                    }}
                }}
                return false;
            }})()
        '''
    })
    return bool(r.get('result'))

def try_click_filtered(exclude_ids):
    """只点击 min_likes 以上的可见卡片"""
    return send_cmd('execute_js', {
        'code': f'''
            (() => {{
                const exclude = new Set({json.dumps(exclude_ids)});
                const minLikes = {CFG['min_likes']};
                if (document.querySelector('.note-detail-mask')) return {{ clicked: false, reason: 'modal open' }};
                const parseCount = (s) => {{
                    s = (s || '').trim();
                    if (!s || s === '赞') return 0;
                    if (s.includes('万')) return Math.round(parseFloat(s) * 10000);
                    const m = s.match(/\\d+/);
                    return m ? parseInt(m[0]) : 0;
                }};
                const cards = document.querySelectorAll('section.note-item');
                const candidates = [];
                for (const card of cards) {{
                    if (card.closest('.note-detail-mask')) continue;
                    let link = null;
                    for (const l of card.querySelectorAll('a[href*="/explore/"]')) {{
                        if (window.getComputedStyle(l).display !== 'none') {{ link = l; break; }}
                    }}
                    if (!link) continue;
                    if (window.getComputedStyle(card).display === 'none') continue;
                    const rect = card.getBoundingClientRect();
                    if (rect.top < 0 || rect.top > window.innerHeight) continue;
                    const m = link.href.match(/\\/explore\\/([a-f0-9]+)/);
                    if (m && exclude.has(m[1])) continue;
                    const likeEl = card.querySelector('.like-wrapper .count') || card.querySelector('.like-wrapper');
                    const likes = parseCount(likeEl ? likeEl.innerText : '');
                    if (likes < minLikes) continue;
                    candidates.push({{ link, likes, id: m ? m[1] : '', text: link.innerText.substring(0, 40), href: link.href }});
                }}
                if (candidates.length > 0) {{
                    const pick = candidates[Math.floor(Math.random() * candidates.length)];
                    pick.link.click();
                    return {{ clicked: true, text: pick.text, id: pick.id, likes: pick.likes, href: pick.href, candidates: candidates.length }};
                }}
                return {{ clicked: false, reason: 'no cards >= ' + minLikes + ' likes in view' }};
            }})()
        '''
    }).get('result', {})

def record_fail(failed_list, channel, note_num, cr, reason):
    failed_list.append({
        '频道': channel,
        '序号': note_num,
        '卡片标题': cr.get('text', ''),
        '卡片赞数': cr.get('likes', ''),
        '笔记ID': cr.get('id', ''),
        '链接': cr.get('href', '') or (f'https://www.xiaohongshu.com/explore/{cr.get("id", "")}' if cr.get('id') else ''),
        '失败原因': reason
    })
    if cr.get('id'):
        return [cr['id']]
    return []

all_results = []
failed_notes = []
time_browse_total = 0.0
time_tech_total = 0.0

# 输出文件名加时间戳，防止覆盖
_ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
_COLLECT_OUTPUT = COLLECT.get('output_file', CFG.get('output_file', 'output/xhs-collect.xlsx'))
_output_file = _COLLECT_OUTPUT.replace('.xlsx', f'-{_ts}.xlsx')

if __name__ == '__main__':
    for ch_cfg in CFG['channels']:
        ch_name = ch_cfg['name']
        ch_count = ch_cfg['note_count']
        print(f'\n{"="*50}')
        print(f'📺 频道: {ch_name}（目标 {ch_count} 篇）')
        print(f'{"="*50}')
    
        # 切换频道
        if not switch_channel(ch_name):
            print(f'  ⚠️ 未找到 "{ch_name}" 频道 tab，尝试直接采集当前 feed')
        else:
            print(f'  ✅ 已切换到 "{ch_name}" 频道')
            time.sleep(rand(B['channel_switch_delay_range']))
    
        collected_ids = []
        fail_streak = 0
        time_browse = 0.0
        time_tech = 0.0
    
        for note_num in range(1, ch_count + 1):
            print(f'\n  📱 [{ch_name}] 第 {note_num} 个笔记')
    
            if is_modal_open():
                send_cmd('back', {})
                time.sleep(rand(COLLECT.get('back_delay_range', [0.8, 2.0])))
    
            # 刷 feed
            cr = {}
            for attempt in range(CFG['max_scroll_attempts']):
                t = time.time()
                for i in range(random.randint(*B['scrolls_per_attempt'])):
                    send_cmd('scroll', {'direction': 'down', 'pixels': random.randint(*B['scroll_px_range'])})
                    time.sleep(rand(B['scroll_pause_range']))
                if random.random() < B['attracted_pause_prob']:
                    time.sleep(rand(B['attracted_pause_range']))
                time_browse += time.time() - t
    
                t = time.time()
                cr = try_click_filtered(collected_ids)
                time_tech += time.time() - t
                if cr.get('clicked'):
                    break
                print(f'    ⏩ 无 ≥{CFG["min_likes"]} 赞笔记，继续刷...')
            else:
                print(f'    ❌ 多次刷屏未找到，跳过')
                ids = record_fail(failed_notes, ch_name, note_num, cr, '多次刷屏未找到符合条件的笔记')
                collected_ids.extend(ids)
                fail_streak += 1
                max_fail = COLLECT.get('max_fail_streak', 3)
                if fail_streak >= max_fail:
                    print(f'  连续失败{max_fail}次，结束该频道')
                    break
                continue
    
            fail_streak = 0
            note_id = cr.get('id', '')
            print(f'    👆 {cr.get("text", "")}（❤️{cr.get("likes", "?")}）')
    
            load_wait = rand(B['page_load_wait_range'])
            time.sleep(load_wait)
            time_browse += load_wait
    
            t = time.time()
            comment_step = random.randint(*B['comment_scroll_step_range'])
            check = send_cmd('execute_js', {
                'code': f'''
                    (() => {{
                        const mask = document.querySelector('.note-detail-mask');
                        if (!mask) return {{ open: false }};
                        if (mask.getBoundingClientRect().width < 300) return {{ open: false }};
                        const s = document.querySelector('.note-scroller');
                        if (s) s.scrollTop += {comment_step};
                        return {{ open: true }};
                    }})()
                '''
            })
            time_tech += time.time() - t
    
            if not check.get('result', {}).get('open'):
                print(f'    ❌ modal 未打开，记入失败表')
                ids = record_fail(failed_notes, ch_name, note_num, cr, 'modal 未打开')
                collected_ids.extend(ids)
                continue
    
            read_time = rand(B['read_time_range'])
            print(f'    📖 阅读 {read_time:.1f}s...')
            time.sleep(read_time)
            time_browse += read_time
    
            # 异常分级探测：CAPTCHA → 整个频道终止，NOTE_GONE → 跳过本笔记
            # 借鉴 MediaCrawler media_platform/xhs/exception.py + client.py:115-154
            exception_class = detect_exception_class()
            if exception_class == 'CAPTCHA':
                label, action = EXCEPTION_MAP['CAPTCHA']
                print(f'    ⛔ {label}（{action}）')
                # 先把当前笔记记失败（保持 Excel 记录连续性），再退出该频道
                record_fail(failed_notes, ch_name, note_num, cr, label)
                break  # 整个频道停：跳出 for note_num，进入下一个频道
            elif exception_class == 'NOTE_GONE':
                label, action = EXCEPTION_MAP['NOTE_GONE']
                print(f'    ⏭️ {label}（{action}）')
                record_fail(failed_notes, ch_name, note_num, cr, label)
                continue  # 跳过本笔记
            elif exception_class == 'MCP_DOWN':
                label, action = EXCEPTION_MAP[exception_class]
                print(f'    🚫 {label}（{action}）')
                record_fail(failed_notes, ch_name, note_num, cr, label)
                continue
    
            if HOT_MODE:
                # §#1 批量滚动 + 单次回传：5-8 次 scroll + 1 次 extract 压缩为 1 次
                # 任务 #2 P1：extract_note() 内部 DOM 优先，失败 fallback 到 INITIAL_STATE
                t = time.time()
                note_data = extract_note(note_id)
                tech_elapsed = time.time() - t
                time_tech += tech_elapsed
    
                if not note_data or 'error' in note_data:
                    print(f'    ❌ 提取失败')
                    record_fail(failed_notes, ch_name, note_num, cr, '数据提取失败')
                    continue
    
                # hot 评分（likes*3 + replies*1）改在 Python 端对 comments[] 排序截取
                wl = COLLECT['hot_weight_likes']
                wr = COLLECT['hot_weight_replies']
                sorted_comments = sorted(
                    note_data.get('comments', []),
                    key=lambda c: c['likes'] * wl + c['replies'] * wr,
                    reverse=True
                )[:CFG['max_comments']]
                note_data['commentText'] = '\n'.join(
                    f"{c['user']}：{c['text']}" for c in sorted_comments
                ) or '无评论'
            else:
                # §#1 批量滚动 + 单次回传：普通模式也通过 extract_note()
                t = time.time()
                note_data = extract_note(note_id)
                tech_elapsed = time.time() - t
                time_tech += tech_elapsed
    
                if not note_data or 'error' in note_data:
                    print(f'    ❌ 提取失败')
                    record_fail(failed_notes, ch_name, note_num, cr, '数据提取失败')
                    continue
    
                # 普通模式：取前 max_comments 条评论拼接为 commentText
                top_comments = note_data.get('comments', [])[:CFG['max_comments']]
                note_data['commentText'] = '\n'.join(
                    f"{c['user']}：{c['text']}" for c in top_comments
                ) or '无评论'
    
            print(f'    {note_data["author"]} | {note_data["title"][:20]} | ❤️{note_data["likeCount"]} ⭐{note_data["collectCount"]} 💬{note_data["commentCount"]} | {tech_elapsed:.2f}s')
    
            record = {
                '频道': ch_name,
                '序号': note_num,
                '笔记作者': note_data['author'],
                '笔记作者_脱敏': mask_nickname(note_data['author']) if DO_MASK else '',
                '笔记作者_哈希': anonymize_user_id(note_data['author']) if DO_ANONYMIZE else '',
                '笔记标题': note_data['title'],
                '笔记内容': note_data['content'],
                '原帖链接': note_data['url'],
                '点赞数': note_data['likeCount'],
                '收藏数': note_data['collectCount'],
                '评论数': note_data['commentCount'],
                '评论内容': note_data['commentText'],
                '图片列表': note_data.get('imageList', ''),
            }
            # === 任务 #9 P1：仅当 GET_SUB=True 时才追加子评论三列 ===
            # 关开关时 record 与改动前完全一致（向后兼容）
            if GET_SUB:
                sub_comments = note_data.get('subComments', []) or []
                record['子评论数'] = len(sub_comments)
                record['子评论内容'] = '; '.join(
                    f"{c['user']}: {c['text']}" for c in sub_comments
                )
                # 父子关系：按 parentId 分组，分配子评论索引 s1, s2, ...
                parent_groups = {}  # parentId -> [sub_idx]
                for idx, c in enumerate(sub_comments, 1):
                    parent_groups.setdefault(c.get('parentId', ''), []).append(idx)
                rel_parts = [
                    f"{pid} -> {', '.join(f's{i}' for i in idxs)}"
                    for pid, idxs in parent_groups.items()
                ]
                record['父子关系'] = '[' + '; '.join(rel_parts) + ']' if rel_parts else ''
            all_results.append(record)
            collected_ids.append(note_id)
    
            gap = rand(B['gap_range'])
            time.sleep(gap)
            time_browse += gap
    
        time_browse_total += time_browse
        time_tech_total += time_tech
        print(f'\n  📊 {ch_name} 完成: {len([r for r in all_results if r["频道"]==ch_name])} 成功 | 浏览{time_browse:.0f}s 技术{time_tech:.0f}s')
    
    # 成表
    t = time.time()
    print(f'\n{"="*50}')
    print(f'💾 全部完成！成功 {len(all_results)} 个，失败 {len(failed_notes)} 个')

    # 尝试用带时间戳的路径保存，PermissionError 时回退到无时间戳路径
    out_dir = os.path.dirname(_output_file)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    try:
        with pd.ExcelWriter(_output_file, engine='openpyxl') as writer:
            if all_results:
                pd.DataFrame(all_results).to_excel(writer, sheet_name='成功采集', index=False)
            if failed_notes:
                pd.DataFrame(failed_notes).to_excel(writer, sheet_name='打开失败', index=False)
    except PermissionError:
        _output_file = _COLLECT_OUTPUT
        print(f'  ⚠️ 时间戳文件被占用，回退到 {_output_file}')
        with pd.ExcelWriter(_output_file, engine='openpyxl') as writer:
            if all_results:
                pd.DataFrame(all_results).to_excel(writer, sheet_name='成功采集', index=False)
            if failed_notes:
                pd.DataFrame(failed_notes).to_excel(writer, sheet_name='打开失败', index=False)
    excel_elapsed = time.time() - t
    time_tech_total += excel_elapsed
    print(f'✅ 已保存到: {_output_file}（成表 {excel_elapsed:.2f}s）')
    print(f'⏱️ 总时间: 拟人浏览 {time_browse_total:.0f}s | 技术操作 {time_tech_total:.0f}s | 技术占比 {time_tech_total/(time_browse_total+time_tech_total)*100:.0f}%')
    if all_results:
        print('\n── 成功采集 ──')
        for row in all_results:
            print(f'  [{row["频道"]}] {row["笔记作者"]} | {row["笔记标题"][:25]} | ❤️{row["点赞数"]} ⭐{row["收藏数"]} 💬{row["评论数"]}')
    if failed_notes:
        print('\n── 打开失败 ──')
        for row in failed_notes:
            print(f'  [{row["频道"]}] {row["卡片标题"][:25]} | ❤️{row["卡片赞数"]} | {row["失败原因"]}')

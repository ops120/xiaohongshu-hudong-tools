"""小红书搜索结果排序切换工具（点击 chip）

P1.30 抽离：原 search.py 内嵌的 SORT_CLICK_JS + SORT_HANDLERS 集中到这里，
供 search.py / reply.py 复用。

职责范围（极简）：
  - 点击「最新」chip 切换 XHS 页面排序
  - 提供注册表，未来加 hot_likes / hot_comments 等只改这里

**不做 Python 端卡片排序** —— 切了「最新」chip 后页面就按时间降序出卡，
再在 Python 端 sort 会和 XHS 内部顺序打架（2026-08 bug P1.31）。
卡片顺序 = XHS 页面顺序 = 已通过 click_js 切换过的排序。

模块本身不耦合 MCP —— 调用方把 run_js（execute_js 解包封装）当参数传入。
失败时返回 dict 供调用方打日志；不主动抛异常（保持 graceful degradation）。
网络异常（MCPDownError 等）由 run_js 上抛，调用方自行 try/except。

参考 cover_utils.py 的「常量 + 注册表 + 函数 + 自测」四段式结构。
"""
import json


# === 排序面板 UI 文案（XHS 改版时同步修改 Python 侧常量 + JS 字符串拼接自动生效） ===
SORT_TRIGGER_LABELS = ('已筛选', '筛选', '综合排序', '排序')  # 触发筛选面板展开的 chip 文本
SORT_ROW_LABEL      = '排序依据'                                  # 排序行容器文本
SORT_LATEST_CHIP    = '最新'                                      # 目标 chip 文本

# === 等待时间（毫秒），命名便于日后调优 ===
SORT_TRIGGER_EXPAND_MS = 800    # 筛选面板展开 + chip 渲染
SORT_REFRESH_MS        = 1500   # SPA 重排结果卡


# === JS 字符串拼接：用 json.dumps(ensure_ascii=False) 把 Python 常量注入 JS ===
# 双引号 JSON 在单引号 JS 字符串里合法；中文不转义可读性更好。
SORT_CLICK_JS = '''
    (async () => {
        const ROW_LABEL       = ''' + json.dumps(SORT_ROW_LABEL, ensure_ascii=False) + ''';
        const CHIP_LABEL      = ''' + json.dumps(SORT_LATEST_CHIP, ensure_ascii=False) + ''';
        const TRIGGER_LABELS  = ''' + json.dumps(list(SORT_TRIGGER_LABELS), ensure_ascii=False) + ''';
        const TRIGGER_RE      = new RegExp('^(' + TRIGGER_LABELS.join('|') + ')$');
        const TRIGGER_EXPAND_MS = ''' + str(SORT_TRIGGER_EXPAND_MS) + ''';
        const SORT_REFRESH_MS   = ''' + str(SORT_REFRESH_MS) + ''';

        const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
        const visible = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        // 激活态：aria-selected=true 或 class 含 active/selected/current（向上回溯 4 层）
        const isActive = (el) => {
            if (!el) return false;
            let node = el;
            for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
                if (node.getAttribute && node.getAttribute('aria-selected') === 'true') return true;
                const cls = (typeof node.className === 'string' ? node.className : '') || '';
                if (/(?:^|[\\s_-])(?:active|selected|current)(?:[\\s_-]|$)/i.test(cls)) return true;
            }
            return false;
        };
        // 找「排序依据」行：实际 DOM 是「label span + chip 容器」两兄弟组成；
        // 找 label 的 parent（行容器）作为 row，而不是 label 自身（label 是 leaf，会让 findChip 空跑）。
        const findRow = () => {
            for (const el of document.querySelectorAll('span, div, li')) {
                if (norm(el.textContent) === ROW_LABEL && el.children.length === 0) {
                    return el.parentElement;
                }
            }
            return null;
        };
        // 找文本精确匹配「最新」的叶子 chip；textContent 避免 innerText 强制 layout
        const findChip = (scope) => {
            let best = null, bestLen = Infinity;
            for (const el of scope.querySelectorAll('*')) {
                if (el.children.length) continue;
                const t = norm(el.textContent);
                if (t === CHIP_LABEL && visible(el) && t.length <= bestLen) { best = el; bestLen = t.length; }
            }
            return best;
        };
        const findTrigger = () => {
            for (const el of document.querySelectorAll('div, span, button')) {
                if (el.children.length) continue;
                const t = norm(el.textContent);
                if (TRIGGER_RE.test(t) && visible(el)) return el;
            }
            return null;
        };
        const fail = (msg) => ({ clicked: false, error: msg });

        // 1. 定位「排序依据」行（面板可能已展开）
        let row = findRow();
        // 2. 未找到或不可见 → 点开排序触发器，等面板渲染后再找
        if (!row || !visible(row)) {
            const trigger = findTrigger();
            if (trigger) {
                trigger.scrollIntoView({ block: 'center' });
                trigger.click();
                await new Promise(r => setTimeout(r, TRIGGER_EXPAND_MS));
            }
            row = findRow();
        }
        if (!row || !visible(row)) {
            return fail('排序依据 row not found');
        }

        // 3. 找「最新」chip（行内搜索）
        const chip = findChip(row);
        if (!chip) {
            return fail('最新 chip not found');
        }

        // 4. 已是激活态 → 不重复点击
        if (isActive(chip)) {
            return { clicked: false, already_selected: true };
        }

        // 5. 滚动到可视区 + 模拟点击 + 等 SPA 重新渲染
        chip.scrollIntoView({ block: 'center' });
        chip.click();
        await new Promise(r => setTimeout(r, SORT_REFRESH_MS));

        return { clicked: true };
    })()
'''


# === 排序注册表：未来加 hot_likes / hot_comments 只改这里 ===
# 只控制「页面要不要切排序 chip」；卡片顺序信任 XHS 页面自身（已在「最新」chip 切换后按时间降序）。
SORT_HANDLERS = {
    'latest': {
        'click_js': SORT_CLICK_JS,        # 点击「最新」chip
    },
    'comprehensive': {
        'click_js': None,                 # 不点击 chip，保持 XHS 默认综合排序
    },
}


def click_filter(run_js, dimension, value):
    """点 XHS「已筛选」面板里的 chip（如 发布时间=一周内）。

    截图观察到的真实 DOM 结构：
      - 筛选面板里有"排序依据 / 笔记类型 / 发布时间 / 搜索范围 / 位置距离" 几行
      - 每行的 chip 直接是文本元素（如 "综合" / "最新" / "一周内" / "视频"）
      - 选中态：chip 带 .red-dot 类（红点标记）

    流程：
      1. 找"__DIMENSION__"行（避免点错维度）
      2. 在该行内找文本为"__VALUE__"的 chip
      3. 检查 red-dot / active / selected → 已选则跳过
      4. 点击

    返回 dict：
      {'clicked': True}                       — 成功点击
      {'clicked': False, 'already_selected': True}  — 已经是当前状态
      {'clicked': False, 'error': '...'}       — 失败
    """
    js = '''
        (async () => {
            const sleep = (ms) => new Promise(res => setTimeout(res, ms));
            const hasDim = () => Array.from(document.querySelectorAll('div, span, button, li')).some(el =>
                el.children.length === 0 && (el.innerText || '').trim() === '__DIMENSION__');
            // 0. 面板未展开时，先点「筛选」触发器展开
            if (!hasDim()) {
                let trigger = null;
                for (const el of document.querySelectorAll('div, span, button')) {
                    if (el.children.length) continue;
                    const t = (el.innerText || '').trim();
                    if (t === '筛选' || t === '已筛选') {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) { trigger = el; break; }
                    }
                }
                if (!trigger) return { error: 'filter trigger not found' };
                trigger.click();
                // 轮询等面板渲染（最多 5s，等「发布时间」标签出现）
                for (let i = 0; i < 15; i++) {
                    await sleep(300);
                    if (hasDim()) break;
                }
            }
            // 1. 找所有 chip 行（每个维度一行）；用文本匹配定位"__DIMENSION__"行
            let rowChip = null;
            for (const el of document.querySelectorAll('div, span, button, li')) {
                if (el.children.length) continue;
                const t = (el.innerText || '').trim();
                if (t !== '__DIMENSION__') continue;
                // rowChip 是维度标签本身；它的 parent 是该行容器
                const row = el.parentElement;
                if (!row) continue;
                rowChip = row;
                break;
            }
            if (!rowChip) return { error: 'dimension label not found: __DIMENSION__' };

            // 2. 在该行内找文本为"__VALUE__"的 chip
            for (const c of rowChip.querySelectorAll('*')) {
                if (c.children.length) continue;
                const t = (c.innerText || '').trim();
                if (t !== '__VALUE__') continue;
                const cls = (c.className || '').toString();
                // XHS 选中态：chip 本身 OR parent 元素 class 含 active / selected / red-dot
                const parentCls = (c.parentElement && c.parentElement.className || '').toString();
                if (/active|selected|red-dot/.test(cls + ' ' + parentCls)) {
                    return { clicked: false, already_selected: true };
                }
                c.click();
                return { clicked: true };
            }
            return { error: 'value chip not found: __VALUE__ in row __DIMENSION__' };
        })()
    '''
    code = js.replace('__DIMENSION__', dimension).replace('__VALUE__', value)
    res = run_js(code)
    if not isinstance(res, dict):
        return {'clicked': False, 'error': f'unexpected JS return: {res!r}'}
    return res


def click_sort(run_js, sort_by='comprehensive'):
    """按 sort_by 切换 XHS 页面排序（只负责点击 chip，不做 Python 端卡片排序）。

    参数：
      run_js:  调用方传入的 execute_js 解包封装；签名 run_js(code: str) -> Any
               （search.py 传 exec_js；reply.py 需自行包装一个 5 行 exec_js）
      sort_by: 排序键（注册表 key；未知值返回 error）

    返回 dict：
      {'clicked': False, 'reason': '...'}                 — 未点击（comprehensive 等）
      {'clicked': False, 'already_selected': True}        — 已是激活态
      {'clicked': False, 'error': '...'}                  — 点击失败 / JS 返回非 dict
      {'clicked': True}                                   — 成功点击

    失败不抛异常；网络异常（MCPDownError 等）由 run_js 上抛，调用方自行 try/except。
    """
    handler = SORT_HANDLERS.get(sort_by)
    if handler is None:
        return {'clicked': False, 'error': f'unknown sort_by: {sort_by}'}
    if handler['click_js'] is None:
        return {'clicked': False, 'reason': f'{sort_by}（不点击 chip）'}
    res = run_js(handler['click_js'])
    if not isinstance(res, dict):
        return {'clicked': False, 'error': f'unexpected JS return: {res!r}'}
    return res


if __name__ == '__main__':
    # 自测：注册表结构 + JS 拼接 + click_sort 分支 + sort_key 行为
    print('SORT_HANDLERS keys:', list(SORT_HANDLERS.keys()))
    print('latest click_js present:', SORT_HANDLERS['latest']['click_js'] is not None)
    print('comprehensive click_js None:', SORT_HANDLERS['comprehensive']['click_js'] is None)
    assert SORT_ROW_LABEL in SORT_CLICK_JS, 'ROW_LABEL not injected'
    assert SORT_LATEST_CHIP in SORT_CLICK_JS, 'CHIP_LABEL not injected'
    assert str(SORT_TRIGGER_EXPAND_MS) in SORT_CLICK_JS, 'TRIGGER_EXPAND_MS not injected'
    assert str(SORT_REFRESH_MS) in SORT_CLICK_JS, 'SORT_REFRESH_MS not injected'
    print('SORT_CLICK_JS injection: OK')

    # click_sort 单测（fake run_js）
    assert click_sort(lambda c: {'clicked': True}, 'latest') == {'clicked': True}
    assert click_sort(lambda c: {'clicked': False, 'already_selected': True}, 'latest') == {'clicked': False, 'already_selected': True}
    assert click_sort(lambda c: None, 'comprehensive') == {'clicked': False, 'reason': 'comprehensive（不点击 chip）'}
    assert 'error' in click_sort(lambda c: None, 'hot_likes')          # 未知值
    assert 'error' in click_sort(lambda c: 'oops', 'latest')            # JS 返回非 dict
    print('click_sort branches: OK')
    print('\nAll sort_utils self-tests passed.')
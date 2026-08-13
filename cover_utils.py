"""小红书封面图工具：下载 + Excel HYPERLINK 关联。

P1.17 抽离：原 like.py P1.16 的 `download_cover()` + 4 个配置 + HYPERLINK helper 集中到这里，
供 like.py / reply.py 复用。

依赖：
  - 仅 urllib + os + 标准库，无 MCP 依赖
  - sns-webpic-qc.xhscdn.com 公开 CDN，无需 Referer / cookies / 登录态
"""
import os
import urllib.request


def load_cover_config(cfg):
    """从 like / reply 节读取封面下载配置。返回命名元组式 dict。"""
    return {
        'enabled': cfg.get('download_covers', False),
        'dir': cfg.get('cover_dir', 'output/covers'),
        'timeout': cfg.get('cover_timeout', 15),
        'max_bytes': cfg.get('cover_max_size_mb', 5) * 1024 * 1024,
    }


def download_cover(url, note_id, cfg):
    """下载小红书封面图到本地。返回相对路径（成功）或空串（失败/跳过）。

    参数：
      url:      CDN URL（sns-webpic-qc.xhscdn.com/.../...!nd_dft_...）
      note_id:  笔记 ID（24 hex），用作文件名
      cfg:      load_config() 返回的 dict

    实现要点：
      - 公开 CDN，无需 Referer / cookies（已 curl 测试通过）
      - 扩展名从 URL 推断（.webp → .webp，其他 → .jpg）
      - 同 note_id 已存在则跳过（幂等，重跑不会重复下载）
      - 超 max_bytes 跳过（避免拖死主循环）
      - 失败仅打日志返回空串，不抛异常
    """
    if not cfg.get('enabled'):
        return ''
    if not url or not note_id:
        return ''
    ext = 'webp' if '.webp' in url.lower() else 'jpg'
    rel_path = f"{cfg['dir'].rstrip('/')}/{note_id}.{ext}"
    abs_dir = os.path.dirname(rel_path) or '.'
    abs_path = os.path.abspath(rel_path)
    if os.path.exists(abs_path):
        # 幂等：已下载过直接返回相对路径
        return rel_path
    try:
        os.makedirs(abs_dir, exist_ok=True)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = urllib.request.urlopen(req, timeout=cfg['timeout'])
        data = resp.read(cfg['max_bytes'] + 1)  # 多读 1 字节用于检测超限
        if len(data) > cfg['max_bytes']:
            print(f'  ⚠️ 封面超过 {cfg["max_bytes"] // 1024 // 1024}MB 上限，跳过：{note_id}')
            return ''
        with open(abs_path, 'wb') as f:
            f.write(data)
        return rel_path
    except Exception as e:
        print(f'  ⚠️ 封面下载失败（{note_id}）: {e}')
        return ''


def cover_hyperlink(local_path):
    """把本地路径包成 Excel HYPERLINK 公式，点击即打开文件。

    返回：
      - 本地路径（如 'output/covers/abc.jpg'） → '=HYPERLINK("file:///D:/.../abc.jpg", "D:/.../abc.jpg")'
      - 空串 → 空串

    显示**绝对路径**（用户要求：方便知道图片具体存哪里、可直接复制），
    点击打开本地图片。
    """
    if not local_path:
        return ''
    abs_path = os.path.abspath(local_path).replace('\\', '/')
    return f'=HYPERLINK("file:///{abs_path}", "{abs_path}")'


# 信息提取 JS（author/title/url/cover）。供 like.py / reply.py 复用。
# 设计要点：
#   P1.18 改造：三级 fallback 解决视频笔记封面提取
#     1) <img> — 图文笔记的封面（modal 内大图）
#     2) <video poster> — 视频元素自带的封面属性
#     3) <meta property="og:image"> — 视频笔记封面的标准位置（HTML head）
#     过滤 picasso-static（小红书平台 logo 占位）和 avatar（头像）
#   P1.19 改造：图片懒加载竞态 — modal 刚开时 <img> 还在加载，
#     改 async 轮询（命中即返回）
#   P1.20 改造：
#     a) mask hydration 等待 — modal 挂载后等 .note-scroller 出现再取值
#     b) 三源合并轮询 — <img> / video.poster / og:image 放进同一循环
#     c) async IIFE 外层 try/catch — 任何 DOM 异常都返回空串
#     d) 轮询常量提为模块级常量
#   P1.21 改造（基于 gpt-5.6-sol 复核）：
#     a) **OG 抢跑修复** — `<meta og:image">` 是 document 全局状态，SPA 切换时旧 OG
#        可能先命中而新 modal 内 <img> 还在加载 → 把上一条封面配给当前标题。
#        修复：OG 检查延迟 `OG_DELAY_ROUNDS=3`（前 0.9s 不查全局 OG），
#        给 modal 内 <img> / poster 优先机会。
#     b) **候选图片语义过滤** — `mask.querySelectorAll('img')` 可能选到头像/推荐图/
#        轮播图；改用 `.note-scroller img`（更精确的 modal 主区域）。
#        同时 src 候选加上 `currentSrc`（响应式 srcset 已加载的 URL），覆盖更多加载状态。
#     c) **最后一轮 sleep 浪费** — 末轮失败后 sleep 300ms 但不再复查，改成只在
#        还有下一轮时才 sleep。
#   P1.22 改造（基于 gpt-5.6-sol 方案设计，避免 timeout 回归）：
#     a) **OG 折中门控** — 保留 OG_DELAY_ROUNDS=3（P1.21 已验证），
#        第 3 轮仅接受相对基线变化的 OG（防旧 OG 抢跑），
#        末轮允许稳定有效 OG 兜底（支持"首次直达"和 video 无 img/poster）。
#        后续采样最多 2 次 / meta 扫描总计最多 3 次（基线 + 第 3 轮 + 末轮）。
#     b) **消除 querySelectorAll** — 全部改用 getElementsByTagName（live collection），
#        避免疑似 timeout 风险。
#     c) **候选链逐项检查** — [currentSrc, getAttribute('src'), data-src] 逐个
#        isPlaceholder，不用 || 短路，避免占位遮住有效候选。
#        注意：不用 img.src 避免空 src 被解析成页面 URL。
#     d) **mask/scroller 每轮重取** — React 替换节点后不会继续用陈旧引用。
#     e) **6.5s 逻辑截止** — 避免无界循环，正常最坏 5.4s；
#        外层 execute_js timeout 才是真硬截止。
#     f) **deadline 前末轮检查** — deadline break 前做一次最终 OG 检查。
EXTRACT_INFO_JS = '''
    (async () => {
        try {
            const POLL_ROUNDS = 10;
            const POLL_INTERVAL_MS = 300;
            const HYDRATION_MAX = 10;
            const OG_DELAY_ROUNDS = 3;     // P1.21 已验证策略
            const HARD_DEADLINE_MS = 6500;
            const startedAt = Date.now();

            const isPlaceholder = (u) =>
                !u ||
                u.startsWith('data:') ||
                u.includes('avatar') ||
                u.includes('picasso-static');

            // 逻辑总时限；避免末轮无意义 sleep
            const waitNext = async () => {
                const remaining = HARD_DEADLINE_MS - (Date.now() - startedAt);
                if (remaining <= 0) return false;
                await new Promise(r =>
                    setTimeout(r, Math.min(POLL_INTERVAL_MS, remaining))
                );
                return Date.now() - startedAt < HARD_DEADLINE_MS;
            };

            // 用 getElementsByTagName 代替 querySelectorAll（避免 timeout 风险）
            const readOgState = () => {
                const valid = [];
                const metas = document.getElementsByTagName('meta');
                for (let i = 0; i < metas.length; i++) {
                    const meta = metas[i];
                    if (meta.getAttribute('property') !== 'og:image') continue;
                    const value = meta.content || meta.getAttribute('content') || '';
                    if (!isPlaceholder(value)) valid.push(value);
                }
                return {
                    key: JSON.stringify(valid),
                    cover: valid.length ? valid[valid.length - 1] : '',
                };
            };

            // 必须在 hydration 前取基线（避免 hydration 期间 OG 已更新漏掉变化）
            // 全程 OG 扫描最多 3 次：基线、第 3 轮、末轮
            const initialOg = readOgState();

            // 1) mask hydration
            let mask = null;
            for (let i = 0; i < HYDRATION_MAX; i++) {
                mask = document.querySelector('.note-detail-mask');
                if (mask && mask.querySelector('.note-scroller')) break;
                if (i < HYDRATION_MAX - 1 && !(await waitNext())) break;
            }
            if (!mask) {
                return { author: '', title: '', url: location.href, cover: '' };
            }

            const txt = (el) =>
                el ? String(el.innerText || el.textContent || '').trim() : '';

            // 2) 封面三源合并轮询
            let cover = '';
            for (let i = 0; i < POLL_ROUNDS; i++) {
                // mask/scroller 每轮重取（最多 10 次）
                const liveMask = document.querySelector('.note-detail-mask');
                if (liveMask) mask = liveMask;
                const scroller = mask.querySelector('.note-scroller');

                // 1) modal <img>（用 getElementsByTagName，live collection）
                if (scroller) {
                    const images = scroller.getElementsByTagName('img');
                    for (let j = 0; j < images.length && !cover; j++) {
                        const img = images[j];
                        // 逐项检查 currentSrc / src / data-src（不用 || 短路）
                        // 注意：用 getAttribute('src') 而非 img.src，
                        // 避免空 src 被浏览器解析成页面 URL
                        const candidates = [
                            img.currentSrc,
                            img.getAttribute('src'),
                            img.getAttribute('data-src'),
                        ];
                        for (const candidate of candidates) {
                            if (!isPlaceholder(candidate)) {
                                cover = candidate;
                                break;
                            }
                        }
                    }
                }

                // 2) video poster
                if (!cover) {
                    const video = mask.querySelector('video');
                    const poster = video
                        ? (video.poster || video.getAttribute('poster') || '')
                        : '';
                    if (!isPlaceholder(poster)) cover = poster;
                }

                // 3) OG 折中门控（仅第 3 轮和末轮检查）
                //    - 延迟 OG_DELAY_ROUNDS 轮（P1.21 已验证策略）
                //    - 第 3 轮：仅接受相对基线变化的 OG（防旧 OG 抢跑）
                //    - 末轮：允许稳定有效 OG（兜底 "首次直达" / video 无 img/poster）
                //    - 后续采样最多 2 次 / meta 扫描总计最多 3 次
                const isBoundedOgCheck =
                    i === OG_DELAY_ROUNDS || i === POLL_ROUNDS - 1;
                if (!cover && i >= OG_DELAY_ROUNDS && isBoundedOgCheck) {
                    const currentOg = readOgState();
                    const changed =
                        currentOg.key !== initialOg.key &&
                        currentOg.cover !== initialOg.cover;
                    const isFinalRound = i === POLL_ROUNDS - 1;

                    if (
                        currentOg.cover &&
                        (changed || isFinalRound)
                    ) {
                        cover = currentOg.cover;
                    }
                }

                if (cover) break;
                if (i < POLL_ROUNDS - 1 && !(await waitNext())) break;
            }

            // deadline 前末轮检查：如果循环因 deadline 退出且无 cover，
            // 再做一次最终 OG 检查（与末轮策略相同）
            if (!cover) {
                const finalOg = readOgState();
                if (finalOg.cover) {
                    cover = finalOg.cover;
                }
            }

            return {
                author: txt(mask.querySelector('.author-container .username')),
                author_id: (() => {
                    const l = mask.querySelector('.author-container a[href*="/user/profile/"]');
                    if (!l) return '';
                    const href = l.getAttribute('href') || '';
                    const m = href.match(/\/user\/profile\/([0-9a-f]+)/);
                    return m ? m[1] : '';
                })(),
                title: txt(mask.querySelector('.note-content .title')),
                url: location.href,
                cover,
            };
        } catch (e) {
            return { author: '', title: '', url: location.href, cover: '', error: String(e) };
        }
    })()
'''

# 向后兼容别名（旧脚本可能还在引用 EXTRACT_COVER_JS）
EXTRACT_COVER_JS = EXTRACT_INFO_JS


# ─── 点赞数提取 JS ───────────────────────────────────────────────────────────
# 从 modal 中读取当前笔记的点赞数，供 like.py / reply.py 复用。
# 返回：数字（如 1234）或空串（未找到）
# 处理 "1.2万" 这种中文单位格式
EXTRACT_LIKE_COUNT_JS = '''
    (() => {
        const mask = document.querySelector('.note-detail-mask');
        if (!mask) return '';
        const likeEl = mask.querySelector('.engage-bar .like-wrapper .count');
        if (!likeEl) return '';
        const text = likeEl.innerText.trim();
        if (text.includes('万')) {
            return Math.round(parseFloat(text) * 10000);
        }
        return parseInt(text) || '';
    })()
'''


def build_log_entry(info, note_id, cover_cfg, send_cmd=None, local_cover=None, extra_fields=None):
    """构建 Excel 日志行的公共逻辑，供 like.py / reply.py 复用。

    自动完成：
      1. 获取点赞数（调用 EXTRACT_LIKE_COUNT_JS，需传 send_cmd）
      2. 下载封面到本地（调用 download_cover，除非已传 local_cover）
      3. 生成封面本地 HYPERLINK（调用 cover_hyperlink）

    参数：
      info:         EXTRACT_INFO_JS 返回的 dict（author/title/url/cover）
      note_id:      笔记 ID（24 hex）
      cover_cfg:    load_cover_config() 返回的配置
      send_cmd:     可选，发送 JS 命令的函数（用于获取点赞数）
      local_cover:  可选，已下载的封面相对路径（跳过下载）
      extra_fields: 可选，额外字段 dict（如 {'状态': '成功', '频道': '穿搭'}）

    返回：dict，可直接追加到 like_log / reply_log
    """
    # 1. 获取点赞数（可选）
    like_count = ''
    if send_cmd:
        try:
            like_count = send_cmd('execute_js', {'code': EXTRACT_LIKE_COUNT_JS}).get('result', '')
        except Exception:
            pass

    # 2. 下载封面（如果未提供 local_cover）
    if local_cover is None:
        local_cover = ''
        if info.get('cover') and note_id:
            local_cover = download_cover(info['cover'], note_id, cover_cfg)
            if local_cover:
                print(f'  📥 封面已下载: {local_cover}')

    # 3. 构建行
    entry = {
        '笔记作者': info.get('author'),
        '笔记标题': info.get('title'),
        '封面图片': info.get('cover', ''),
        '封面图片_本地': cover_hyperlink(local_cover),
        '链接': info.get('url'),
        '点赞数': like_count,
    }
    if extra_fields:
        entry.update(extra_fields)

    return entry
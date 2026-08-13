"""异常分支文案常量 — 借鉴 MediaCrawler 异常分级思路

格式:
    EXCEPTION_MAP[code] = (label, action)

- label:  打印到日志的用户可见中文文案
- action: 处理建议（不强制代码遵循，仅供人工参考）

注意:
- 这是常量，不引入 ORM / dataclass，便于直接在日志/UI 中读 label。
- 与 `exceptions.py` 的异常类一一对应，但使用字符串 key 是因为
  部分异常（CAPTCHA / NOTE_GONE）是浏览器内 JS 探测得到的，无法 raise。
"""
# (label, action) — 与 docs/research-mc-borrowing-detail.md §#3 一致
EXCEPTION_MAP = {
    'IP_BLOCKED': ('风控：IP 被封',         '暂停 30 分钟或换 IP'),
    'NOTE_GONE':  ('笔记已删除',              '跳过本笔记继续'),
    'CAPTCHA':    ('触发滑块验证',            '立即终止，提示用户手动处理'),
    'MCP_DOWN':   ('BrowserMCP 无响应',       '重试 3 次后终止'),
}


def explain(code):
    """便捷查询：返回 (label, action)，未知 code 返回 ('未知异常', '')。"""
    return EXCEPTION_MAP.get(code, ('未知异常', ''))


if __name__ == '__main__':
    # 单元自测：列出全部映射 + 查未知 code
    print('=== exception_map.py 映射表 ===\n')
    for code, (label, action) in EXCEPTION_MAP.items():
        print(f'  {code:12} -> {label:20} | 处理: {action}')

    print('\n[explain() 边界自测]')
    cases = [
        ('CAPTCHA', ('触发滑块验证', '立即终止，提示用户手动处理')),
        ('NOTE_GONE', ('笔记已删除', '跳过本笔记继续')),
        ('UNKNOWN_X', ('未知异常', '')),  # 兜底
    ]
    all_ok = True
    for code, expected in cases:
        got = explain(code)
        ok = got == expected
        all_ok = all_ok and ok
        print(f"  explain({code!r:12}) = {got!r:40} {'✅' if ok else '❌'}")
    print(f'\n=== {"全部通过 ✅" if all_ok else "有失败 ❌"} ===')

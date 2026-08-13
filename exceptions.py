"""异常分级映射 — 借鉴 MediaCrawler media_platform/xhs/exception.py

提供:
- XhsException: 所有本项目异常的基类
- IPBlockedError:    风控封禁（HTTP 461/471，code 300012）
- NoteNotFoundError: 笔记已删除 / 页面不存在（code -510000 / -510001）
- CaptchaError:      触发滑块验证（页面含「请通过验证」）
- MCPDownError:      BrowserMCP 无响应（URLError / timeout）

与 `exception_map.EXCEPTION_MAP` 配合使用，本文件只定义类，分支文案在 map 中。
通过 `python exceptions.py` 打印继承层次总览。
"""


class XhsException(Exception):
    """小红书自动化所有异常的基类（便于上层 except 一把抓）。"""
    pass


class IPBlockedError(XhsException):
    """风控：IP 被封（HTTP 461/471，code 300012）。处理：暂停 30 分钟或换 IP。"""
    pass


class NoteNotFoundError(XhsException):
    """笔记已删除 / 页面不存在（code -510000 / -510001）。处理：跳过本笔记继续。"""
    pass


class CaptchaError(XhsException):
    """触发滑块验证（页面含「请通过验证」）。处理：立即终止，提示用户手动。"""
    pass


class MCPDownError(XhsException):
    """BrowserMCP 无响应（URLError / timeout）。处理：重试 3 次后终止。"""
    pass


if __name__ == '__main__':
    # 打印继承层次
    print('=== exceptions.py 异常类层次 ===\n')
    classes = [
        ('XhsException', XhsException),
        ('IPBlockedError', IPBlockedError),
        ('NoteNotFoundError', NoteNotFoundError),
        ('CaptchaError', CaptchaError),
        ('MCPDownError', MCPDownError),
    ]
    for name, cls in classes:
        bases = [b.__name__ for b in cls.__bases__]
        print(f'  {name:20} <-- {", ".join(bases)}')

    # 基本行为自测：每个子类都应能 raise + catch 为 XhsException
    print('\n[继承一致性自测]')
    all_ok = True
    for name, cls in classes[1:]:  # 跳过基类
        try:
            raise cls('test ' + name)
        except XhsException as e:
            ok = type(e).__name__ == name
            all_ok = all_ok and ok
            print(f"  raise {name:20} -> caught as XhsException: {'✅' if ok else '❌'} ({type(e).__name__})")
    print(f'\n=== {"全部通过 ✅" if all_ok else "有失败 ❌"} ===')

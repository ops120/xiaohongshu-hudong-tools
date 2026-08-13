"""隐私硬化工具 — 借鉴 MediaCrawler tools/user_hash.py

提供:
- mask_nickname(name): 昵称脱敏，首尾 1 字 + '***'（极短名称退化为全 '*'）
- anonymize_user_id(uid): 用户 ID 哈希化，sha256 截断 16 位（关联友好）

通过 `python privacy.py` 直接运行单元自测。
"""
import hashlib


def mask_nickname(name):
    """首尾 1 字 + '***'（len<2 时全 '*'，len==0 时返回 ''）。

    示例:
        '小红薯D41559EC' -> '小***C'
        '欧阳'            -> '欧***阳'
        ''                -> ''
        'X'               -> '*'
    """
    name = (name or '').strip()
    if not name:
        return ''
    if len(name) < 2:
        return '*' * len(name)
    return name[0] + '***' + name[-1]


def anonymize_user_id(uid):
    """对 user_id 做 sha256，截断 16 位 hex。

    相同 uid 多次调用结果一致，便于跨次运行关联同一用户。
    uid 为空（None / ''）时返回 ''。

    示例:
        '5f8a3b...' -> 'a1b2c3d4e5f60718'
    """
    if not uid:
        return ''
    return hashlib.sha256(uid.encode('utf-8')).hexdigest()[:16]


def apply_privacy(record, do_mask, do_anonymize, user_id=''):
    """对单条输出 dict 应用隐私字段（不修改原字段，向后兼容）。

    返回新增的字段 dict，可与原 record 合并:
        new = apply_privacy(...)
        record.update(new)

    新增字段:
        - 笔记作者_脱敏 / 作者_脱敏: mask_nickname 后结果
        - 笔记作者_哈希 / 作者_哈希: anonymize_user_id 后结果

    参数:
        record: 原始 dict
        do_mask: bool，是否脱敏昵称
        do_anonymize: bool，是否哈希 user_id
        user_id: 用于哈希的 ID（默认空，常见于聊天页只拿到昵称）
    """
    return {
        '脱敏': mask_nickname(record.get('笔记作者', record.get('作者', ''))) if do_mask else '',
        '哈希': anonymize_user_id(user_id) if do_anonymize else '',
    }


if __name__ == '__main__':
    # 单元自测
    print('=== privacy.py 单元自测 ===')

    # mask_nickname 测试
    cases = [
        ('小红薯D41559EC', '小***C'),
        ('欧阳', '欧***阳'),
        ('', ''),
        ('X', '*'),
        (None, ''),
        ('小红薯', '小***薯'),
        ('AB', 'A***B'),
    ]
    print('\n[mask_nickname]')
    all_ok = True
    for inp, expected in cases:
        got = mask_nickname(inp)
        ok = got == expected
        all_ok = all_ok and ok
        print(f"  mask_nickname({inp!r:20}) = {got!r:10} 期望={expected!r:10} {'✅' if ok else '❌'}")

    # anonymize_user_id 测试
    print('\n[anonymize_user_id]')
    uids = [
        '5f8a3b0c1d2e3f4a5b6c7d8e',
        'user-12345',
        '5f8a3b0c1d2e3f4a5b6c7d8e',  # 重复，应得到相同结果
        '',
        None,
    ]
    for uid in uids:
        r1 = anonymize_user_id(uid)
        r2 = anonymize_user_id(uid)
        consistent = r1 == r2
        ok = (uid and len(r1) == 16 and consistent) or (not uid and r1 == '')
        all_ok = all_ok and ok
        print(f"  anonymize_user_id({uid!r:30}) = {r1!r:20} 一致={'✅' if consistent else '❌'} 合法={'✅' if ok else '❌'}")

    # 关联性测试：同 uid 多次 hash 一致
    print('\n[一致性] 相同 uid 多次 hash:')
    uid = 'test-user-fixed-id'
    hashes = [anonymize_user_id(uid) for _ in range(3)]
    consistent = len(set(hashes)) == 1
    all_ok = all_ok and consistent
    print(f"  {hashes} {'✅' if consistent else '❌'}")

    print(f'\n=== {"全部通过 ✅" if all_ok else "有失败 ❌"} ===')

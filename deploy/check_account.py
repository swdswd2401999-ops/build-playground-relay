#!/usr/bin/env python3
"""在本机诊断账号凭据，判断能不能推到服务器。

这个脚本不推送 —— 推送用官方的 `aistudio2api setup --login --remote <origin>`，
它会同时上传 Camoufox 指纹（自己拼 JSON 调 pairing 接口很容易漏掉指纹，
漏了之后账号能导入但指纹不一致，反而更容易被风控）。

它只做一件事：推之前告诉你这份凭据到底行不行。

用法：
  python check_account.py auth/user@gmail.com
  python check_account.py --all          # 扫整个 auth 目录
"""
import argparse
import json
import os
import sys
import time

REQUIRED = ['SAPISID', '__Secure-1PSID', '__Secure-3PSID', 'SID']
MIN_COOKIES = 20
# 续签时这些 cookie 的 mtime/值会变，太久没动说明续签停了
MAX_AGE_H = 12


def check_one(d):
    d = d.rstrip('/')
    name = os.path.basename(d)
    ss_path = os.path.join(d, 'storage-state.json')
    rows = []
    ok = True

    if not os.path.isfile(ss_path):
        return name, False, [('storage-state.json', False, '缺失')]

    try:
        ss = json.load(open(ss_path, encoding='utf-8'))
    except Exception as e:
        return name, False, [('JSON', False, f'解析失败: {e}')]

    cookies = ss.get('cookies') or []
    origins = ss.get('origins') or []

    # 1. cookie 数量
    good = len(cookies) >= MIN_COOKIES
    rows.append(('cookies', good, f'{len(cookies)} 个（建议 ≥{MIN_COOKIES}）'))
    ok &= good

    # 2. 关键 cookie —— 服务端校验会明确点名缺哪个
    names = {c.get('name') for c in cookies}
    for need in REQUIRED:
        hit = need in names
        if not hit:
            ok = False
            rows.append((need, False, '缺失 ← 服务端会拒绝'))
    if all(n in names for n in REQUIRED):
        rows.append(('关键 cookie', True, 'SAPISID / PSID 系列齐全'))

    # 3. origins —— 这是"能不能续签"的唯一判据
    has_ai = any('aistudio.google.com' in (o.get('origin') or '') for o in origins)
    rows.append(('origins', has_ai,
                 f'{len(origins)} 个' + ('' if has_ai else
                 ' ← 没有 aistudio.google.com，续签代码永远启动不了')))
    if not has_ai:
        ok = False

    # 4. 浏览器指纹（官方 setup 会一起传，缺了不影响导入但影响一致性）
    fp = os.path.join(d, 'camoufox-fingerprint.json')
    rows.append(('camoufox 指纹', os.path.isfile(fp),
                 '有' if os.path.isfile(fp) else '无（用小体积环境时可能触发风控）'))

    # 5. 续签新鲜度
    age_h = (time.time() - os.path.getmtime(ss_path)) / 3600
    fresh = age_h <= MAX_AGE_H
    rows.append(('最后刷新', fresh, f'{age_h:.1f}h 前'))

    # 6. 绑了不可达代理
    rs = os.path.join(d, 'runtime-state.json')
    if os.path.isfile(rs):
        try:
            p = (json.load(open(rs, encoding='utf-8')).get('proxy') or '').strip()
        except Exception:
            p = ''
        if p:
            import re
            import subprocess
            m = re.search(r'://([^:/]+):(\d+)', p)
            if m:
                host, port = m.group(1), m.group(2)
                r = subprocess.run(
                    ['bash', '-c', f'timeout 2 bash -c "</dev/tcp/{host}/{port}" 2>/dev/null'],
                    capture_output=True)
                alive = r.returncode == 0
                rows.append(('绑定代理', alive,
                             f'{p} ' + ('可达' if alive else '← 不可达，启用代理池后会失联')))

    return name, ok, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('path', nargs='?', help='账号目录，如 auth/user@gmail.com')
    ap.add_argument('--all', action='store_true', help='扫描 path 下所有账号')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args()

    targets = []
    if a.all:
        root = a.path or 'auth'
        if not os.path.isdir(root):
            sys.exit(f'找不到目录 {root}')
        targets = [os.path.join(root, n) for n in sorted(os.listdir(root))
                   if os.path.isdir(os.path.join(root, n))
                   and not n.startswith('.')]
    elif a.path:
        targets = [a.path]
    else:
        sys.exit('给一个账号目录，或 --all')

    bad = 0
    for d in targets:
        name, ok, rows = check_one(d)
        mark = '✅' if ok else '❌'
        if not ok:
            bad += 1
        if a.quiet and ok:
            print(f'{mark} {name}')
            continue
        print(f'\n{mark} {name}')
        for label, good, detail in rows:
            print(f'   {"·" if good else "✗"} {label:16} {detail}')

    print()
    if bad:
        print(f'{bad}/{len(targets)} 个账号不能推。')
        print('修复方式：aistudio2api setup --login --remote <服务器> 重新隔离登录。')
        print('不要用 Chrome 导入 —— Chrome 导入永远拿不到 origins。')
    else:
        print(f'{len(targets)}/{len(targets)} 个账号可推。')
        print('推送：aistudio2api setup --login --remote <服务器>')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())

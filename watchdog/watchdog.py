#!/usr/bin/env python3
"""双通道反代健康看门狗。

每轮检查：
  1. systemd 服务活着
  2. /api/status 的 ready 账号数 >= 1
  3. 真实推理能成功
  4. 每个账号的 storage-state 是否具备**续签能力**（origins 非空）
  5. 是否绑了不存在的代理

只在状态变化时写日志，避免每 5 分钟刷屏。

为什么第 4 项单独查：账号"能用"和"能续签"是两件事。Chrome 导入的账号
origins 为空，续签代码根本启动不了，但 cookie 可能还有 300 多天才过期 ——
这期间它一直是 ready，请求正常，只有日志里每 5 分钟失败一次。等 cookie
真过期那天才集体掉线，那时已经来不及了。

探针模型必须避开业务主力：每 5 分钟一次 = 288 次/天，打主力模型会吃掉
一半以上的日配额（实测 100 次/天的账号，探针占 30 次）。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

LOG = os.environ.get('WATCHDOG_LOG', '/var/log/aistudio-watchdog.log')
STATE = os.environ.get('WATCHDOG_STATE', '/opt/aistudio2api/health.json')
INSTALL_DIR = os.environ.get('AISTUDIO_DIR', '/opt/aistudio2api')
ENV = os.path.join(INSTALL_DIR, '.env')
AUTH = os.path.join(INSTALL_DIR, 'auth')
SERVICE = os.environ.get('AISTUDIO_SERVICE', 'aistudio2api')
# 探针模型：选业务不用的那个。配错会让监控吃掉真实配额。
PROBE_MODEL = os.environ.get('WATCHDOG_PROBE_MODEL', 'gemini-3-flash-preview')


def log(msg):
    line = f'{time.strftime("%Y-%m-%d %H:%M:%S")}  {msg}'
    print(line)
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def read_key():
    try:
        with open(ENV, encoding='utf-8') as f:
            m = re.search(r'^PROXY_API_KEY=(.*)$', f.read(), re.M)
        return m.group(1).strip() if m else ''
    except Exception:
        return ''


def scan_accounts():
    """扫每个账号的 storage-state，判断能不能续签。

    origins 里必须有 aistudio.google.com —— 没有它就拿不到 AuthExtension，
    续签第一步就失败。
    """
    out = {}
    if not os.path.isdir(AUTH):
        return out
    for name in sorted(os.listdir(AUTH)):
        f = os.path.join(AUTH, name, 'storage-state.json')
        if not os.path.isfile(f):
            continue
        try:
            ss = json.load(open(f, encoding='utf-8'))
        except Exception:
            out[name] = {'origins': -1, 'renewable': False}
            continue
        origins = ss.get('origins') or []
        has_ai = any('aistudio.google.com' in (o.get('origin') or '')
                     for o in origins)
        out[name] = {
            'origins': len(origins),
            'cookies': len(ss.get('cookies') or []),
            'renewable': bool(has_ai),
            'age_h': round((time.time() - os.path.getmtime(f)) / 3600, 1),
        }
    return out


def scan_dead_proxy():
    """找出绑了不存在代理的账号。

    runtime-state.json 里可能残留 "proxy": "http://127.0.0.1:7890" 这类绑定
    （从本机导入时带过来的）。PROXY 为空时它不生效，但一旦启用代理池，
    这些账号会立刻全部失联，且报错指向"网络错误"，很容易误判成封号。
    """
    dead = []
    if not os.path.isdir(AUTH):
        return dead
    for name in sorted(os.listdir(AUTH)):
        rs = os.path.join(AUTH, name, 'runtime-state.json')
        if not os.path.isfile(rs):
            continue
        try:
            d = json.load(open(rs, encoding='utf-8'))
        except Exception:
            continue
        p = (d.get('proxy') or '').strip()
        if not p:
            continue
        m = re.search(r'://([^:/]+):(\d+)', p)
        if not m:
            continue
        host, port = m.group(1), m.group(2)
        r = subprocess.run(['bash', '-c',
                            f'timeout 2 bash -c "</dev/tcp/{host}/{port}" 2>/dev/null'],
                           capture_output=True)
        if r.returncode != 0:
            dead.append(f'{name} -> {p}')
    return dead


def check():
    result = {'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'ok': False, 'reasons': []}

    active = subprocess.run(['systemctl', 'is-active', SERVICE],
                            capture_output=True).stdout.decode().strip()
    result['service'] = active
    if active != 'active':
        result['reasons'].append(f'service={active}')

    try:
        with urllib.request.urlopen('http://127.0.0.1:2048/api/status',
                                    timeout=20) as r:
            st = json.loads(r.read().decode())
        acc = st.get('accounts') or {}
        result['state'] = st.get('state')
        result['ready'] = acc.get('ready')
        result['total'] = acc.get('total')
    except Exception as e:
        result['reasons'].append(f'status_{type(e).__name__}')
        result['ok'] = False
        return result

    if not result['ready']:
        result['reasons'].append('ready=0')

    if result['ready']:
        key = read_key()
        body = json.dumps({'model': PROBE_MODEL,
                           'messages': [{'role': 'user', 'content': '1'}],
                           'max_tokens': 64}).encode()
        req = urllib.request.Request(
            'http://127.0.0.1:2048/v1/chat/completions', data=body,
            headers={'Authorization': f'Bearer {key}',
                     'Content-Type': 'application/json',
                     'User-Agent': 'Mozilla/5.0'})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                r.read()
                result['inference'] = 'ok'
                result['latency_ms'] = int((time.time() - t0) * 1000)
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode('utf-8', 'replace')[:200]
            result['inference'] = f'http_{e.code}'
            result['reasons'].append(f'inference_{e.code}')
            # 把上游的原始错误留下来 —— 判断"该重新登录"还是"额度用完了"
            # 全靠这段文字，压缩成状态码等于把线索丢掉
            result['upstream_error'] = body_txt
        except Exception as e:
            result['inference'] = type(e).__name__
            result['reasons'].append(f'inference_{type(e).__name__}')

    accounts = scan_accounts()
    result['accounts'] = accounts
    result['not_renewable'] = sorted(
        n for n, v in accounts.items() if not v.get('renewable'))
    result['stale'] = sorted(
        n for n, v in accounts.items()
        if v.get('renewable') and v.get('age_h', 0) > 12)
    result['dead_proxy'] = scan_dead_proxy()

    result['ok'] = not result['reasons']
    return result


def main():
    r = check()

    prev = {}
    try:
        prev = json.load(open(STATE, encoding='utf-8'))
    except Exception:
        pass

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, 'w', encoding='utf-8') as f:
        json.dump(r, f, ensure_ascii=False, indent=2)

    # 结构性失效：只在名单变化时记
    cur_dead = set(r.get('not_renewable') or [])
    prev_dead = set(prev.get('not_renewable') or [])
    for n in sorted(cur_dead - prev_dead):
        info = (r.get('accounts') or {}).get(n, {})
        log(f'账号不可续签  {n}  origins={info.get("origins")}  '
            f'cookies={info.get("cookies")}  ← 需重新隔离登录')
    for n in sorted(prev_dead - cur_dead):
        log(f'账号恢复续签能力  {n}')

    # 续签疑似停止：可续签的账号 mtime 太久没动
    for n in r.get('stale') or []:
        if n not in (prev.get('stale') or []):
            info = (r.get('accounts') or {}).get(n, {})
            log(f'账号续签疑似停止  {n}  {info.get("age_h")}h 未刷新')

    # 死代理绑定（只在 PROXY 启用时才是真故障）
    if r.get('dead_proxy') and r.get('dead_proxy') != prev.get('dead_proxy'):
        log(f'账号绑了不可达代理（当前 PROXY 为空所以无害，'
            f'但启用代理后会立刻失联）: {r["dead_proxy"]}')

    if r['ok']:
        # 只在状态真正变化时写日志。第一次运行（无历史状态）也记一次，
        # 否则新装的机器永远看不到"一切正常"的基线
        if not prev:
            log(f"首次检查通过  账号 {r.get('ready')}/{r.get('total')}  "
                f"延迟 {r.get('latency_ms')}ms")
        elif not prev.get('ok'):
            log(f"从异常恢复  账号 {r.get('ready')}/{r.get('total')}  "
                f"延迟 {r.get('latency_ms')}ms")
        elif prev.get('ready') != r.get('ready'):
            log(f"账号数变化 {prev.get('ready')} → {r.get('ready')}"
                f"/{r.get('total')}")
        print('OK')
        return 0

    log(f"异常  原因={','.join(r['reasons'])}  账号 {r.get('ready')}/{r.get('total')}")
    if r.get('upstream_error'):
        log(f'    上游原文: {r["upstream_error"][:180]}')
    return 1


if __name__ == '__main__':
    sys.exit(main())

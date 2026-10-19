"""抓本地栈真实 API 契约 -> JSON 结构摘要（不打印敏感值）"""
import json, os, sys, urllib.request, urllib.error

BASE = "http://127.0.0.1:8100"
PWD_FILE = r"PATH_TO/admin_password.txt"

pwd = [l.split(":", 1)[1].strip() for l in open(PWD_FILE, encoding="utf-8") if l.startswith("password:")][0]


def call(path, method="GET", body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


st, login = call("/api/auth/login", "POST", {"email": "admin@juxing.local", "password": pwd})
print("login:", st, list(login.keys()) if isinstance(login, dict) else login)
tok = login.get("token") or login.get("access_token")
if not tok:
    sys.exit("no token")

ENDPOINTS = [
    "/api/auth/me", "/api/usage/summary", "/api/usage/requests?limit=3",
    "/api/keys", "/api/me/credentials?mode=antigravity", "/api/me/credentials?mode=geminicli",
    "/api/admin/overview", "/api/admin/users?limit=3&query=", "/api/admin/tiers",
    "/api/admin/codes?limit=3", "/api/admin/error-messages", "/api/admin/events?limit=3",
]


def shape(v, depth=0):
    if depth > 2:
        return "…"
    if isinstance(v, dict):
        return {k: shape(x, depth + 1) for k, x in list(v.items())[:14]}
    if isinstance(v, list):
        return [shape(v[0], depth + 1)] if v else []
    if isinstance(v, str):
        return f"str[{len(v)}]" if len(v) > 40 else v
    return v if v is not None else None


out = {}
for p in ENDPOINTS:
    st, body = call(p, token=tok)
    key = p.split("?")[0]
    out[key] = {"status": st, "shape": shape(body) if st == 200 else str(body)[:200]}
    print(f"\n=== {p} [{st}]")
    print(json.dumps(out[key]["shape"], ensure_ascii=False, indent=1)[:1400])

with open(r".\api_contract.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("\nsaved -> api_contract.json")

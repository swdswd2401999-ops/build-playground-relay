#!/usr/bin/env bash
# 在本机（Windows/macOS/Linux 桌面）登录账号并推送到服务器。
#
# 为什么必须用 --login 而不是 Chrome 导入：
#   Chrome 导入只给 cookie，不给 origins（aistudio.google.com 的 localStorage）。
#   没有 origins 就拿不到 AuthExtension，续签代码启动第一步就失败。
#   账号现在能用，但 cookie 到期那天直接掉线，中间没有任何征兆。
#   实测：Chrome 导入的账号 origins=0，Camoufox 隔离登录的 origins=2。
set -euo pipefail

SERVER=""
EMAIL=""
ADDR="127.0.0.1:2048"

usage() {
    cat <<'EOF'
用法：
  bash login-account.sh --server <服务器地址> [--email <邮箱>] [--tunnel <端口>]

  --server   服务器地址，如 https://relay.example.com
             如果服务器只监听 127.0.0.1（默认），先建隧道：
                 ssh -N -L 2048:127.0.0.1:2048 user@server
             然后 --server http://127.0.0.1:2048

  --email    要登录的邮箱。省略则打开浏览器让你自己选账号

示例：
  # 先开隧道（另开一个终端，保持不关）
  ssh -N -L 2048:127.0.0.1:2048 root@1.2.3.4

  # 再登录并推送
  bash login-account.sh --server http://127.0.0.1:2048

⚠️ 会弹出真实的浏览器窗口（Camoufox，Firefox 内核），需要你手动完成 Google 登录。
   登录完成后状态会自动上传，窗口自动关闭。
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --server) SERVER="$2"; shift 2 ;;
        --email) EMAIL="$2"; shift 2 ;;
        --tunnel) ADDR="127.0.0.1:$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage; exit 2 ;;
    esac
done

[ -n "$SERVER" ] || { usage; exit 2; }

# 找二进制
BIN="${AISTUDIO_BIN:-}"
if [ -z "$BIN" ]; then
    for c in ./aistudio2api ./aistudio2api.exe aistudio2api aistudio2api.exe; do
        if [ -x "$c" ] || [ -f "$c" ]; then BIN="$c"; break; fi
    done
fi
[ -n "$BIN" ] || {
    echo "找不到 aistudio2api 二进制。" >&2
    echo "把它放到这个脚本同目录，或设置 AISTUDIO_BIN=<路径>" >&2
    exit 1
}

echo "二进制: $BIN"
echo "服务器: $SERVER"
echo

# 先探一下服务器通不通 —— 隧道没建时这里就会失败，比等到登录完才报错好
if ! curl -s -m 8 -o /dev/null -w '%{http_code}' "$SERVER/api/status" 2>/dev/null | grep -qE '^(200|401|403)$'; then
    echo "连不上 $SERVER" >&2
    echo >&2
    echo "如果服务器只监听 127.0.0.1，先在另一个终端建隧道：" >&2
    echo "    ssh -N -L 2048:127.0.0.1:2048 <user>@<server>" >&2
    echo "然后：bash $0 --server http://127.0.0.1:2048" >&2
    exit 1
fi
echo "服务器可达"
echo

ARGS=(setup --login --remote "$SERVER")
[ -n "$EMAIL" ] && ARGS+=(--email "$EMAIL")

echo "开始隔离登录（会弹出浏览器窗口）…"
echo
"$BIN" "${ARGS[@]}"

echo
echo "完成。到服务器上验证："
echo "    curl -s http://127.0.0.1:2048/api/status"
echo "    python3 /opt/aistudio2api/../check_account.py --all /opt/aistudio2api/auth"

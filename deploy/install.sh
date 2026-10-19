#!/usr/bin/env bash
# Build/Playground 双通道反代 — 服务器一键安装
#
# 用法：
#   sudo bash install.sh                    # 默认装到 /opt/aistudio2api
#   sudo bash install.sh --dir /srv/relay   # 自定义目录
#   sudo bash install.sh --port 2049        # 换端口（默认 2048）
#
# 前置：Linux x86_64，systemd，已下载 aistudio2api 二进制放在同目录
# 它会做：建用户 → 装二进制 → 写 .env → 装 systemd → 起服务 → 自检
set -euo pipefail

INSTALL_DIR=/opt/aistudio2api
SERVICE_USER=aistudio
UNIT_NAME=aistudio2api
LISTEN_PORT=2048
BIN_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/aistudio2api"

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) INSTALL_DIR="$2"; shift 2 ;;
        --user) SERVICE_USER="$2"; shift 2 ;;
        --unit) UNIT_NAME="$2"; shift 2 ;;
        --port) LISTEN_PORT="$2"; shift 2 ;;
        --bin) BIN_SRC="$2"; shift 2 ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "需要 root（用 sudo）"
[ -f "$BIN_SRC" ] || die "找不到二进制: $BIN_SRC
  去 https://github.com/Mag1cFall/AIStudio2API/releases 下载 linux-amd64 版本，
  解压后把 aistudio2api 放到本脚本同目录。"

case "$INSTALL_DIR" in
    /tmp/*|/var/tmp/*)
        die "不要装到 $INSTALL_DIR
  unit 里 PrivateTmp=yes 会把 /tmp 换成私有 tmpfs，
  ReadWritePaths= 指向的路径在挂载命名空间里不存在，
  服务会以 status=226/NAMESPACE 反复失败。换到 /opt 或 /srv。"
        ;;
esac

say "安装目录: $INSTALL_DIR"
say "运行用户: $SERVICE_USER"

# ── 1. 用户 ────────────────────────────────────────────────
if id "$SERVICE_USER" >/dev/null 2>&1; then
    say "用户 $SERVICE_USER 已存在"
else
    say "创建系统用户 $SERVICE_USER"
    useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# 家目录必须有：程序会在 $HOME/.cache 下写浏览器指纹缓存与运行时锁。
# 缺家目录时表现为「mkdir /home/xxx: permission denied」然后无限重启。
HOME_DIR="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
[ -d "$HOME_DIR" ] || { mkdir -p "$HOME_DIR"; chown "$SERVICE_USER:$SERVICE_USER" "$HOME_DIR"; }
# .cache 必须提前建好：systemd 的 ReadWritePaths= 指向不存在的目录时，
# 服务根本起不来（status=226/NAMESPACE, "Failed to set up mount namespacing"）。
# 这不是权限问题，是 mount namespace 建不起来。
mkdir -p "$HOME_DIR/.cache"
chown -R "$SERVICE_USER:$SERVICE_USER" "$HOME_DIR/.cache"
say "家目录: $HOME_DIR（.cache 已就绪）"

# ── 2. 目录与二进制 ────────────────────────────────────────
say "准备目录"
install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$INSTALL_DIR"
install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$INSTALL_DIR/auth" \
        "$INSTALL_DIR/runtime"
install -m 755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$BIN_SRC" "$INSTALL_DIR/aistudio2api"
say "二进制已装: $INSTALL_DIR/aistudio2api"

# 端口占用检查：装之前就知道，比启动后看到 address already in use 省事
if ss -ltn 2>/dev/null | grep -q ":$LISTEN_PORT "; then
    OCC=$(ss -ltnp 2>/dev/null | grep ":$LISTEN_PORT " | head -1)
    if ! echo "$OCC" | grep -q "$UNIT_NAME"; then
        echo "⚠️  端口 $LISTEN_PORT 已被占用："
        echo "    $OCC"
        echo "    继续安装会和它冲突。换个端口： --port 2049"
        echo
    fi
fi

# ── 3. .env ────────────────────────────────────────────────
if [ -f "$INSTALL_DIR/.env" ]; then
    say ".env 已存在，保留原文件（不覆盖）"
else
    say "生成 .env（含随机 PROXY_API_KEY）"
    API_KEY="sk-$(head -c 48 /dev/urandom | base64 | tr -d '/+=' | head -c 47)"
    cat > "$INSTALL_DIR/.env" <<EOF
# ── 监听与认证 ────────────────────────────────────────────
LISTEN_ADDR=127.0.0.1:$LISTEN_PORT
PROXY_API_KEY=$API_KEY
# 管理 API 免登录仅限 loopback；若要把 2048 暴露到公网，必须改成 true
ADMIN_AUTH_ENABLED=false

# ── 账号 ──────────────────────────────────────────────────
AISTUDIO_AUTH_STATES=auth
AUTO_START=true

# ── 后端 ──────────────────────────────────────────────────
# go = 纯 Go，不起浏览器（~350MB/4账号）；camoufox = 起真浏览器（~700MB/账号）
WAA_BACKEND=go

# 双通道：build 优先（更快），playground 兜底
UPSTREAM_CHANNELS=build,playground
# true = 非流式请求优先走 Build 通道（ProxyUnaryCall，原生非流式，快）
# false = 严格按 UPSTREAM_CHANNELS 顺序 → 非流式会走 Playground，
#         而 Playground 是流式收集后转非流式，实测慢一个数量级（3s → 22s+）
BUILD_NATIVE_NONSTREAM=true
ROUTING_STRATEGY=round-robin

# ── 并发 ──────────────────────────────────────────────────
# 单账号同时执行的请求数。调高会加速撞日限额（429 按账号+模型计）
PER_ACCOUNT_CONCURRENCY=2
# 常驻预热账号数，设成可用账号数即可
WARM_WORKER_LIMIT=2
MAX_ACTIVE_WORKERS=4
WARM_STARTUP_CONCURRENCY=2

# ── 日志 ──────────────────────────────────────────────────
LOG_FILE=true
LOG_MAX_TOTAL_MB=200

# ── 超时（默认 30 分钟偏大，与反向代理的读超时容易不一致）────────
REQUEST_TIMEOUT=10m
EOF
    chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
    say "PROXY_API_KEY 已生成，查看：sudo grep PROXY_API_KEY $INSTALL_DIR/.env"
fi

# ── 4. systemd ─────────────────────────────────────────────
say "安装 systemd unit"
cat > /etc/systemd/system/$UNIT_NAME.service <<EOF
[Unit]
Description=AIStudio2API (Build/Playground 双通道反代)
After=network-online.target
Wants=network-online.target
# 连续失败熔断：RestartSec=5 会让 systemd 默认的「5次/10秒」永远达不到，
# 必须显式放宽窗口，否则永久性启动失败会无限重试
StartLimitIntervalSec=300
StartLimitBurst=6

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/aistudio2api
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Go runtime 不认 cgroup 的 MemoryHigh（只看 GOGC），必须用 GOMEMLIMIT
# 让 GC 主动工作在限制以下，否则表现为「没到上限却突然卡顿」
Environment=GOMEMLIMIT=900MiB
MemoryHigh=1G
MemoryMax=1200M

# 进程退出时一并结束子进程
KillMode=mixed
TimeoutStopSec=20

# 沙箱：它只写 auth/ 与 ~/.cache
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$INSTALL_DIR/auth $INSTALL_DIR/runtime $HOME_DIR/.cache

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable $UNIT_NAME >/dev/null 2>&1
say "unit 已装并设为开机自启: $UNIT_NAME.service"

# ── 5. 启动与自检 ──────────────────────────────────────────
say "启动服务"
systemctl restart $UNIT_NAME
sleep 8

if ! systemctl is-active --quiet $UNIT_NAME; then
    echo
    echo "服务没起来，最近日志："
    journalctl -u $UNIT_NAME -n 25 --no-pager
    exit 1
fi

say "服务已 active，等待就绪…"
for i in $(seq 1 20); do
    ST=$(curl -s -m 5 http://127.0.0.1:$LISTEN_PORT/api/status 2>/dev/null || true)
    # 0 账号时 state 是 STOPPED，这是正常的 —— 还没有账号可跑
    if echo "$ST" | grep -qE '"state":"(RUNNING|STOPPED)"'; then
        echo "  $ST"
        break
    fi
    sleep 3
done

echo
say "安装完成"
cat <<EOF

下一步（还没有账号，所以 ready 会是 0）：

  1. 在**有图形界面的机器**（Windows/macOS）上登录账号：
       aistudio2api setup --login --remote http://<本机>:$LISTEN_PORT
     ⚠️ 必须用 --login（Camoufox 隔离登录），不要用 Chrome 导入 ——
        Chrome 导入的 storage-state 里 origins 是空的，Linux 上永远无法续签。

  2. 服务器目前只监听 127.0.0.1。要让本机推账号上来，二选一：
       a) SSH 隧道（推荐）：ssh -N -L $LISTEN_PORT:127.0.0.1:$LISTEN_PORT user@server
       b) 临时反代到 HTTPS（用完关掉）

  3. 验证：
       curl -s http://127.0.0.1:$LISTEN_PORT/api/status
       curl -H "Authorization: Bearer \$(grep ^PROXY_API_KEY $INSTALL_DIR/.env | cut -d= -f2-)" \\
            http://127.0.0.1:$LISTEN_PORT/v1/models

  4. 装健康看门狗（可选但强烈建议）：
       sudo bash ../watchdog/install-watchdog.sh

查看日志： journalctl -u $UNIT_NAME -f
配置文件： $INSTALL_DIR/.env
EOF

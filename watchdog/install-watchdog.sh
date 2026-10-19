#!/usr/bin/env bash
# 安装健康看门狗：每 5 分钟跑一次，只在状态变化时写日志。
set -euo pipefail

INSTALL_DIR=/opt/aistudio2api
SERVICE_USER=aistudio
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "需要 root" >&2; exit 1; }

install -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" \
    "$HERE/watchdog.py" "$INSTALL_DIR/watchdog.py"

cat > /etc/systemd/system/aistudio-watchdog.service <<EOF
[Unit]
Description=AIStudio2API 健康看门狗
After=aistudio2api.service

[Service]
Type=oneshot
User=$SERVICE_USER
Environment=AISTUDIO_DIR=$INSTALL_DIR
Environment=WATCHDOG_PROBE_MODEL=${PROBE_MODEL:-gemini-3-flash-preview}
ExecStart=/usr/bin/python3 $INSTALL_DIR/watchdog.py
EOF

cat > /etc/systemd/system/aistudio-watchdog.timer <<'EOF'
[Unit]
Description=每 5 分钟检查一次

[Timer]
# 首次延迟 2 分钟，避开服务启动期的正常波动
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now aistudio-watchdog.timer

echo "看门狗已装。日志：/var/log/aistudio-watchdog.log"
echo "立即跑一次试试： sudo systemctl start aistudio-watchdog.service"

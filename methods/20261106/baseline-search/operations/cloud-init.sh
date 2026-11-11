#!/bin/bash
set -euo pipefail
install -d -m 755 /etc/systemd/system
cat > /etc/systemd/system/yggdrisil-baseline-budget-stop.timer <<'TIMER'
[Unit]
Description=Baseline absolute approved budget stop
[Timer]
OnCalendar=2026-11-07 15:48:07 UTC
Persistent=true
Unit=yggdrisil-baseline-budget-stop.service
[Install]
WantedBy=timers.target
TIMER
cat > /etc/systemd/system/yggdrisil-baseline-budget-stop.service <<'SERVICE'
[Unit]
Description=Stop dedicated baseline worker at approved cap
[Service]
Type=oneshot
ExecStart=/usr/sbin/shutdown -h now
SERVICE
systemctl daemon-reload
systemctl enable --now yggdrisil-baseline-budget-stop.timer
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-dev build-essential libglpk40
sudo -u ubuntu python3 -m venv /home/ubuntu/yggdrisil-baselines-20261106/env
sudo -u ubuntu /home/ubuntu/yggdrisil-baselines-20261106/env/bin/python -m pip install uv==0.11.31

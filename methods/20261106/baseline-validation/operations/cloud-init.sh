#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/yggdrisil-validation-bootstrap.log) 2>&1
cat > /etc/systemd/system/yggdrisil-baseline-validation-stop.service <<'SERVICE'
[Unit]
Description=Approved dedicated validation budget stop
[Service]
Type=oneshot
ExecStart=/usr/sbin/shutdown -h now
SERVICE
cat > /etc/systemd/system/yggdrisil-baseline-validation-stop.timer <<'TIMER'
[Unit]
Description=Absolute validation spending deadline
[Timer]
OnCalendar=2026-11-07 17:19:39 UTC
Persistent=true
Unit=yggdrisil-baseline-validation-stop.service
[Install]
WantedBy=timers.target
TIMER
systemctl daemon-reload
systemctl enable --now yggdrisil-baseline-validation-stop.timer
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y build-essential git curl unzip openjdk-21-jre-headless pkg-config libopenblas-dev liblapack-dev libglpk-dev python3-venv python3-dev
install -d -o ubuntu -g ubuntu /home/ubuntu/yggdrisil-baseline-validation-20261106
sudo -u ubuntu python3 -m venv /home/ubuntu/yggdrisil-baseline-validation-20261106/env
sudo -u ubuntu /home/ubuntu/yggdrisil-baseline-validation-20261106/env/bin/pip install uv==0.11.31
sudo -u ubuntu bash -c 'set -euo pipefail; cd /home/ubuntu/yggdrisil-baseline-validation-20261106; git clone https://github.com/CovertLab/vEcoli.git native; cd native; git checkout b2078bd8e226c5d319bb9ddaa10a1f2f1fcfdbbc; ../env/bin/uv python install 3.12.12; ../env/bin/uv sync --frozen --no-dev --python 3.12.12'
install -d -o ubuntu -g ubuntu /home/ubuntu/yggdrisil-baseline-validation-20261106/bin
sudo -u ubuntu bash -c 'set -euo pipefail; cd /home/ubuntu/yggdrisil-baseline-validation-20261106/bin; curl -fsSL https://github.com/nextflow-io/nextflow/releases/download/v25.10.4/nextflow -o nextflow; chmod 755 nextflow; NXF_VER=25.10.4 ./nextflow -version'
touch /home/ubuntu/yggdrisil-baseline-validation-20261106/bootstrap.ready

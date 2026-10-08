#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 24.04 EC2 instance (US region).
# Usage: bash deploy/setup.sh   (from the repo root, as the ubuntu user)
set -euo pipefail
sudo apt-get update -y
sudo apt-get install -y python3-venv chrony
sudo systemctl enable --now chrony          # NTP: accurate clock for latency numbers
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "--- probing DraftKings from this server ---"
.venv/bin/python scripts/probe.py --states IL,NJ,VA --listen 30 || echo "PROBE FAILED: see output above before going further"
sudo cp deploy/betstamp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now betstamp
echo "App running on 127.0.0.1:8000. Next: install Caddy and use deploy/Caddyfile for HTTPS."

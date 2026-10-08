#!/bin/bash
# EC2 user data (Ubuntu 24.04). Runs once as root on first boot and sets
# everything up unattended. Progress goes to the serial console, which you
# can read with: aws ec2 get-console-output --latest --instance-id <id>
exec > >(tee -a /var/log/odds-setup.log /dev/console) 2>&1
echo "ODDS-SETUP start $(date -u)"
export DEBIAN_FRONTEND=noninteractive
REPO=git@github.com:vincent0955/draftkings-live-odds.git
U=/home/ubuntu

apt-get update -y
apt-get install -y python3-venv git chrony curl gnupg debian-keyring debian-archive-keyring apt-transport-https
systemctl enable --now chrony   # NTP-synced clock: the latency numbers depend on it

# Caddy: reverse proxy with automatic HTTPS
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y && apt-get install -y caddy

# Read-only deploy key for the private repo. Only the public half leaves the box.
sudo -u ubuntu ssh-keygen -q -t ed25519 -N "" -C ec2-draftkings-odds -f $U/.ssh/deploy_key
sudo -u ubuntu bash -c "ssh-keyscan -t ed25519 github.com >> $U/.ssh/known_hosts 2>/dev/null"
printf 'Host github.com\n  IdentityFile ~/.ssh/deploy_key\n  IdentitiesOnly yes\n' | sudo -u ubuntu tee -a $U/.ssh/config >/dev/null
# Raw 32-byte key as hex (easy to copy exactly) + a short checksum to verify the copy.
HEX=$(awk '{print $2}' $U/.ssh/deploy_key.pub | base64 -d | tail -c 32 | od -An -tx1 | tr -d ' \n')
SUM=$(printf %s "$HEX" | sha256sum | cut -c1-8)
( for i in $(seq 1 40); do echo "DEPLOYKEY $HEX SUM $SUM"; sleep 30; done ) &
KEYLOOP=$!

until sudo -u ubuntu git clone -q $REPO $U/betstamp 2>/dev/null; do sleep 10; done
kill $KEYLOOP 2>/dev/null
echo "ODDS-SETUP cloned"

cd $U/betstamp
sudo -u ubuntu python3 -m venv .venv
sudo -u ubuntu .venv/bin/pip install -q -r requirements.txt
sudo -u ubuntu .venv/bin/python scripts/probe.py --states IL,NJ,VA --listen 20 2>&1 | sed 's/^/PROBE /'

cp deploy/betstamp.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now betstamp

TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)
HOST="$(echo "$IP" | tr . -).sslip.io"
printf '%s {\n\treverse_proxy 127.0.0.1:8000 {\n\t\tflush_interval -1\n\t}\n}\n' "$HOST" > /etc/caddy/Caddyfile
systemctl reload caddy
sleep 20
echo "ODDS-SETUP health: $(curl -s -m 5 http://127.0.0.1:8000/health | head -c 300)"
echo "ODDS-SETUP done https://$HOST"

#!/bin/bash
# Run in AWS CloudShell, next to run.sh:   bash set-proxy.sh
# Asks for the proxy URL (input hidden), stores it on the server in a
# root-only file, pulls the latest code, probes DraftKings through the proxy
# and restarts the app. The proxy is used for the REST snapshot only.
read -r -s -p "Proxy URL (http://user:pass@host:port or socks5://...): " PROXY; echo
[ -z "$PROXY" ] && { echo "no proxy given, nothing changed"; exit 1; }
B64=$(printf %s "$PROXY" | base64 -w0)
cat > /tmp/set-proxy-remote.sh <<EOF
set -e
export P="\$(echo $B64 | base64 -d)"
mask() { python3 -c "import os,sys; p=os.environ['P']; sys.stdout.write(sys.stdin.read().replace(p, '<proxy>'))"; }
cd /home/ubuntu/betstamp
sudo -u ubuntu git pull -q
sudo -u ubuntu .venv/bin/pip install -q -r requirements.txt
install -d -m 700 /etc/betstamp
umask 077
printf 'DK_HTTP_PROXY=%s\n' "\$P" > /etc/betstamp/proxy.env
mkdir -p /etc/systemd/system/betstamp.service.d
printf '[Service]\nEnvironmentFile=/etc/betstamp/proxy.env\n' > /etc/systemd/system/betstamp.service.d/proxy.conf
systemctl daemon-reload
echo "--- probe through the proxy"
DK_HTTP_PROXY="\$P" sudo --preserve-env=DK_HTTP_PROXY -u ubuntu .venv/bin/python scripts/probe.py --states IL --listen 5 2>&1 | mask | grep -E "REST|HTTP|OK|FAIL|RESULT|games"
systemctl restart betstamp
sleep 15
echo "--- app after restart"
curl -s http://127.0.0.1:8000/health | python3 -c "import json,sys; d=json.load(sys.stdin); print('state:', d['state'], '| board source:', d['snapshot'], '| games:', d['games']); print(*d['log'][-2:], sep='\n')" | mask
EOF
bash run.sh /tmp/set-proxy-remote.sh
rm -f /tmp/set-proxy-remote.sh /tmp/params.json

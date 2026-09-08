#!/usr/bin/env bash
# Idempotent, pre-restart installation. Existing Flask stays up on failure.
set -euo pipefail
project_dir="${MAXCOURSE_PROJECT_DIR:-/www/wwwroot/maxcourse}"
cap_base="/opt/maxcourse-cap"
node_bin="/opt/maxcourse-media/node/bin/node"
npm_cli="/opt/maxcourse-media/node/lib/node_modules/npm/bin/npm-cli.js"
test -x "$node_bin"
test -f "$npm_cli"
test -f "$project_dir/cap_service/server.mjs"
umask 022
fingerprint=$(cat "$project_dir/cap_service/package-lock.json" "$project_dir/cap_service/server.mjs" | sha256sum | cut -c1-20)
release_dir="$cap_base/releases/$fingerprint"
mkdir -p "$release_dir"
if [ ! -f "$release_dir/.ready" ]; then
    cp "$project_dir/cap_service/package.json" "$project_dir/cap_service/package-lock.json" "$project_dir/cap_service/server.mjs" "$release_dir/"
    (cd "$release_dir" && "$node_bin" "$npm_cli" ci --omit=dev --ignore-scripts --no-audit --no-fund)
    touch "$release_dir/.ready"
fi
if [ ! -f /etc/maxcourse-cap.env ]; then
    (umask 077; python3 - <<'PY'
import secrets
with open('/etc/maxcourse-cap.env', 'x') as file:
    file.write('MAXCOURSE_CAP_SECRET=' + secrets.token_hex(32) + '\n')
    file.write('MAXCOURSE_CAP_URL=http://127.0.0.1:5068\n')
PY
    )
fi
chmod 600 /etc/maxcourse-cap.env
install -m 644 "$project_dir/deploy/maxcourse-cap.service" /etc/systemd/system/maxcourse-cap.service
mkdir -p /etc/systemd/system/maxcourse.service.d
printf '[Service]\nEnvironmentFile=/etc/maxcourse-cap.env\n' > /etc/systemd/system/maxcourse.service.d/cap.conf
old_release=$(readlink "$cap_base/current" || true)
ln -sfn "$release_dir" "$cap_base/.next"
mv -Tf "$cap_base/.next" "$cap_base/current"
systemctl daemon-reload
systemctl enable maxcourse-cap.service >/dev/null
if [ "$old_release" != "$release_dir" ]; then
    systemctl restart maxcourse-cap.service
else
    systemctl start maxcourse-cap.service
fi
# Do not log the bridge credential. Verify readiness before restarting Flask.
python3 - <<'PY'
from pathlib import Path
import json, time, urllib.request
config=dict(line.split('=',1) for line in Path('/etc/maxcourse-cap.env').read_text().splitlines() if '=' in line)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
request=urllib.request.Request('http://127.0.0.1:5068/health', headers={'X-Cap-Service-Secret':config['MAXCOURSE_CAP_SECRET']})
for attempt in range(20):
    try:
        with opener.open(request, timeout=2) as response:
            if json.load(response).get('ok') is True:
                print('Cap verifier ready')
                break
    except Exception:
        time.sleep(.25)
else:
    raise SystemExit('Cap verifier did not become ready; keep the existing Flask process running')
PY

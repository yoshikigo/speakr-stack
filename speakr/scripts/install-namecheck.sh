#!/usr/bin/env bash
# 人名查證：Speakr 摘要完成 → webhook → speakr-url 側車用 SearXNG + Ollama 查證人名、修正同音錯字、附來源。
# 需要先跑過 install-url-import.sh（共用 speakr-url 容器）。可重複執行。
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
QUADLET_DIR="$HOME/.config/containers/systemd"
SPEAKR_ENV="$HOME/.config/speakr/speakr.env"
SEARXNG=${SEARXNG_URL:-http://127.0.0.1:8090}
OLLAMA=${OLLAMA_URL:-http://127.0.0.1:11435}
SPEAKR=http://127.0.0.1:8899
HOOK_URL=http://127.0.0.1:8897/hooks/speakr

ok()   { printf '  \e[32m✔\e[0m %s\n' "$*"; }
warn() { printf '  \e[33m!\e[0m %s\n' "$*"; }
die()  { printf '  \e[31m✘\e[0m %s\n' "$*"; exit 1; }

echo "== 1/6 前置檢查"
[ -f "$QUADLET_DIR/speakr-url.container" ] || die "先跑 ./scripts/install-url-import.sh"
curl -fsS "$SEARXNG/search?q=test&format=json" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null \
  || die "SearXNG ($SEARXNG) 沒有回 JSON：在 SearXNG 的 settings.yml 的 search.formats 加上 json 後重啟"
ok "SearXNG JSON API"
MODEL=$(sed -n 's/^TEXT_MODEL_NAME=//p' "$SPEAKR_ENV")
curl -fsS "$OLLAMA/api/tags" | grep -q "\"$MODEL\"" || warn "Ollama 清單裡沒看到 $MODEL（名稱含 :latest 時可忽略）"
ok "Ollama $OLLAMA，模型 $MODEL（和 Speakr 摘要共用，不會互相換模型）"

echo "== 2/6 Speakr API token（podman secret speakr-api-token）"
if podman secret exists speakr-api-token 2>/dev/null; then
  TOKEN=$(podman secret inspect --showsecret --format '{{.SecretData}}' speakr-api-token)
  ok "已存在"
else
  cat <<'TXT'
  在 Speakr：右上角頭像 → Account Settings → API Tokens → 建立新 token
    名稱：namecheck；權限選 Custom，勾 read、write、webhooks；有效期限自訂
TXT
  read -rsp "  貼上 token: " TOKEN; echo
  [ -n "$TOKEN" ] || die "沒有輸入"
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$SPEAKR/api/v1/users/me")
[ "$code" = 200 ] || die "token 無效或權限不足（HTTP $code）"
podman secret exists speakr-api-token 2>/dev/null || printf '%s' "$TOKEN" | podman secret create speakr-api-token - >/dev/null
ok "token 可用"

echo "== 3/6 允許 Speakr 把 webhook 送到本機"
if ! grep -q '^WEBHOOK_INTRANET_HOST_ALLOWLIST=' "$SPEAKR_ENV"; then
  printf '\n# 人名查證 webhook 送到本機的 speakr-url\nWEBHOOK_INTRANET_HOST_ALLOWLIST=^127\\.0\\.0\\.1$\n' >> "$SPEAKR_ENV"
  systemctl --user restart speakr.service
  for _ in $(seq 1 60); do curl -fsS -o /dev/null "$SPEAKR/login" && break; sleep 2; done
fi
ok "$(grep '^WEBHOOK_INTRANET_HOST_ALLOWLIST=' "$SPEAKR_ENV")"

echo "== 4/6 建立 webhook（recording.summary.completed → $HOOK_URL）"
SECRET=$(TOKEN="$TOKEN" SPEAKR="$SPEAKR" HOOK_URL="$HOOK_URL" HAVE_SECRET="$(podman secret exists speakr-webhook-secret 2>/dev/null && echo 1 || echo 0)" python3 - <<'PY'
import json, os, sys, urllib.request
base, tok, url = os.environ["SPEAKR"], os.environ["TOKEN"], os.environ["HOOK_URL"]
def call(method, path, body=None):
    req = urllib.request.Request(f"{base}/api/v1{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)
listing = call("GET", "/webhooks")
hooks = listing.get("webhooks", listing.get("data", [])) if isinstance(listing, dict) else listing
mine = next((h for h in hooks if h.get("name") == "namecheck"), None)
if mine and os.environ["HAVE_SECRET"] == "1":
    if mine.get("url") != url:
        call("PATCH", f"/webhooks/{mine['id']}", {"url": url, "allow_http": True,
                                                    "events": ["recording.summary.completed"], "enabled": True})
    print("")          # 沿用既有 secret
elif mine:
    print(call("POST", f"/webhooks/{mine['id']}/rotate-secret")["secret"])
else:
    created = call("POST", "/webhooks", {"name": "namecheck", "url": url, "allow_http": True, "enabled": True,
                                         "events": ["recording.summary.completed"]})
    print(created["secret"])
PY
)
if [ -n "$SECRET" ]; then
  podman secret rm speakr-webhook-secret >/dev/null 2>&1 || true
  printf '%s' "$SECRET" | podman secret create speakr-webhook-secret - >/dev/null
  ok "webhook 已建立，secret 存成 podman secret speakr-webhook-secret"
else
  ok "webhook 已存在，沿用"
fi
unset TOKEN SECRET

echo "== 5/6 speakr-url 加上查證設定（drop-in）並重建"
mkdir -p "$QUADLET_DIR/speakr-url.container.d"
cat > "$QUADLET_DIR/speakr-url.container.d/namecheck.conf" <<EOF
[Container]
Secret=speakr-api-token,type=env,target=SPEAKR_API_TOKEN
Secret=speakr-webhook-secret,type=env,target=SPEAKR_WEBHOOK_SECRET
Environment=SEARXNG_URL=$SEARXNG
Environment=OLLAMA_URL=$OLLAMA
Environment=NAMECHECK_MODEL=$MODEL
Environment=TZ=Asia/Taipei
EOF
podman build -t localhost/speakr-url:latest --build-arg YTDLP_REFRESH=keep "$HERE/url-import"
systemctl --user daemon-reload
systemctl --user restart speakr-url.service speakr.service
for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:8897/api/health 2>/dev/null | grep -Eq '"namecheck": ?true' && break; sleep 2; done
curl -fsS http://127.0.0.1:8897/api/health | grep -Eq '"namecheck": ?true' || { journalctl --user -u speakr-url -n 30 --no-pager; die "speakr-url 沒讀到 token"; }
ok "speakr-url 已啟用人名查證"

echo "== 6/6 對既有錄音試跑（可略過）"
read -rp "  輸入要查證的錄音 ID（網址 /recording/<ID>；直接 Enter 略過）: " RID
if [ -n "$RID" ]; then
  podman exec speakr-url /opt/url-import/bin/python /opt/url-import/namecheck.py "$RID"
fi

echo
echo "完成。之後每次摘要產生完，約 1–2 分鐘內摘要最後會多一段「人名查證（自動）」。"
echo "手動重跑：podman exec speakr-url /opt/url-import/bin/python /opt/url-import/namecheck.py <錄音ID>"

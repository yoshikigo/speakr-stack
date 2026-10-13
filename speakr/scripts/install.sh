#!/usr/bin/env bash
# Speakr 一鍵部署：檢查 asr-server / Ollama → 設定 → Quadlet → tailscale serve (HTTPS :8899)
# 可重複執行；已存在的 secret / 設定檔不會被覆蓋。
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
PORT=8899
TS_PORT=${TS_PORT:-8899}
CONF_DIR="$HOME/.config/speakr"
QUADLET_DIR="$HOME/.config/containers/systemd"
ASR_URL=http://127.0.0.1:8098
OLLAMA_URL=http://127.0.0.1:11435

ok()   { printf '  \e[32m✔\e[0m %s\n' "$*"; }
warn() { printf '  \e[33m!\e[0m %s\n' "$*"; }
die()  { printf '  \e[31m✘\e[0m %s\n' "$*"; exit 1; }

echo "== 1/6 前置檢查"
curl -fsS "$ASR_URL/health" >/dev/null || die "asr-server 沒回應（systemctl --user status asr-server）"
ok "asr-server $ASR_URL"
curl -fsS "$OLLAMA_URL/v1/models" >/dev/null || die "Ollama proxy 沒回應（$OLLAMA_URL）"
ok "Ollama proxy $OLLAMA_URL"
if ss -ltn "( sport = :$PORT )" | grep -q "127.0.0.1:$PORT\|\*:$PORT\|0.0.0.0:$PORT"; then
  podman ps --format '{{.Names}}' | grep -qx speakr || die "127.0.0.1:$PORT 已被其他程式占用（ss -ltnp | grep $PORT）"
fi
ok "port $PORT 可用"
command -v tailscale >/dev/null || die "找不到 tailscale"
TS_HOST=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
[ -n "$TS_HOST" ] || die "抓不到 tailnet 主機名稱（MagicDNS / HTTPS 憑證有開嗎？）"
ok "tailnet 主機 $TS_HOST"
if tailscale serve status 2>/dev/null | grep -q ":$TS_PORT\b" && ! tailscale serve status | grep -A2 ":$TS_PORT\b" | grep -q "127.0.0.1:$PORT"; then
  die "tailscale serve 的 :$TS_PORT 已經給別的服務用了（tailscale serve status），改用 TS_PORT=xxxx ./scripts/install.sh"
fi

echo "== 2/6 設定檔 $CONF_DIR/speakr.env"
mkdir -p "$CONF_DIR"
if [ -f "$CONF_DIR/speakr.env" ]; then
  warn "已存在，不覆蓋（新版範本在 $HERE/config/speakr.env）"
else
  sed "s#^APP_BASE_URL=.*#APP_BASE_URL=https://$TS_HOST:$TS_PORT#" "$HERE/config/speakr.env" > "$CONF_DIR/speakr.env"
  chmod 600 "$CONF_DIR/speakr.env"
  ok "已建立（APP_BASE_URL=https://$TS_HOST:$TS_PORT）"
fi

MODEL=$(sed -n 's/^TEXT_MODEL_NAME=//p' "$CONF_DIR/speakr.env")
if command -v ollama >/dev/null; then
  CTX=$(ollama show "$MODEL" --parameters 2>/dev/null | awk '$1=="num_ctx"{print $2}')
  if [ -z "$CTX" ]; then
    warn "$MODEL 沒設 num_ctx（或模型不存在）→ Ollama 會用很小的預設 context，長逐字稿會被截斷"
  elif [ "$CTX" -lt 32768 ]; then
    warn "$MODEL 的 num_ctx=$CTX，建議 ≥ 32768"
  else
    ok "$MODEL num_ctx=$CTX"
  fi
fi

echo "== 3/6 管理員密碼（podman secret speakr-admin-password）"
if podman secret exists speakr-admin-password 2>/dev/null; then
  ok "已存在"
else
  read -rsp "  設定 Speakr 管理員（$(sed -n 's/^ADMIN_USERNAME=//p' "$CONF_DIR/speakr.env")）密碼: " PW; echo
  [ ${#PW} -ge 8 ] || die "密碼至少 8 個字元"
  printf '%s' "$PW" | podman secret create speakr-admin-password - >/dev/null
  unset PW
  ok "已建立"
fi

echo "== 4/6 拉映像（約 700 MB）"
podman pull docker.io/learnedmachine/speakr:lite
podman image inspect --format '  digest: {{.Digest}}' docker.io/learnedmachine/speakr:lite

echo "== 5/6 Quadlet"
mkdir -p "$QUADLET_DIR"
cp "$HERE/quadlet/speakr.container" "$QUADLET_DIR/"
# 已經裝過網址匯入（含校正姓名、繁中翻譯）就沿用打過補丁的映像，不要被換回官方映像
if podman image exists localhost/speakr-url:latest && [ -f "$QUADLET_DIR/speakr-url.container" ]; then
  sed -i 's#^Image=.*#Image=localhost/speakr-url:latest#' "$QUADLET_DIR/speakr.container"
  echo "  （沿用 localhost/speakr-url:latest；要更新 Speakr 版本請跑 ./scripts/install-url-import.sh）"
fi
systemctl --user daemon-reload
systemctl --user restart speakr.service
printf '  等待 Speakr 起來'
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/login"; then echo; ok "http://127.0.0.1:$PORT"; break; fi
  printf '.'; sleep 3
done
curl -fsS -o /dev/null "http://127.0.0.1:$PORT/login" || { echo; journalctl --user -u speakr -n 40 --no-pager; die "Speakr 沒起來"; }

echo "== 6/6 tailscale serve → https://$TS_HOST:$TS_PORT"
if ! tailscale serve --bg --https="$TS_PORT" "http://127.0.0.1:$PORT" 2>/dev/null; then
  warn "需要 root（或先執行 sudo tailscale set --operator=$USER）"
  sudo tailscale serve --bg --https="$TS_PORT" "http://127.0.0.1:$PORT"
fi
tailscale serve status

echo
echo "完成 → https://$TS_HOST:$TS_PORT  （帳號：$(sed -n 's/^ADMIN_USERNAME=//p' "$CONF_DIR/speakr.env")）"

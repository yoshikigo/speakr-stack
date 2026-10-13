#!/usr/bin/env bash
# 在已部署的 Speakr 上加「從網址匯入（yt-dlp）」：
#   build localhost/speakr-url → Speakr 改用此映像 → 啟動 speakr-url 側車 → tailscale serve 掛 /url-import
# 可重複執行（也是 Speakr / yt-dlp 更新後的重建方式）。
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
QUADLET_DIR="$HOME/.config/containers/systemd"
TS_PORT=${TS_PORT:-8899}
IMAGE=localhost/speakr-url:latest

ok()   { printf '  \e[32m✔\e[0m %s\n' "$*"; }
warn() { printf '  \e[33m!\e[0m %s\n' "$*"; }
die()  { printf '  \e[31m✘\e[0m %s\n' "$*"; exit 1; }

echo "== 1/5 前置檢查"
[ -f "$QUADLET_DIR/speakr.container" ] || die "找不到 speakr.container，先跑 ./scripts/install.sh"
ss -ltn | grep -q '127.0.0.1:8897 ' && ! podman ps --format '{{.Names}}' | grep -qx speakr-url \
  && die "127.0.0.1:8897 已被占用"
ok "OK"

echo "== 2/5 build $IMAGE（第一次會下載 yt-dlp + deno，約 100 MB）"
podman pull docker.io/learnedmachine/speakr:lite
podman build -t "$IMAGE" --build-arg "YTDLP_REFRESH=${YTDLP_REFRESH:-$(date +%F)}" "$HERE/url-import"
ok "$(podman run --rm --entrypoint /opt/url-import/bin/yt-dlp "$IMAGE" --version | sed 's/^/yt-dlp /')"

echo "== 3/5 Speakr 改用 $IMAGE"
cp -n "$QUADLET_DIR/speakr.container" "$QUADLET_DIR/speakr.container.bak-url" || true
sed -i "s#^Image=.*#Image=$IMAGE#" "$QUADLET_DIR/speakr.container"
cp "$HERE/quadlet/speakr-url.container" "$QUADLET_DIR/"
systemctl --user daemon-reload
systemctl --user restart speakr.service speakr-url.service

printf '  等待服務'
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null http://127.0.0.1:8899/login && curl -fsS -o /dev/null http://127.0.0.1:8897/api/health; then
    echo; ok "speakr + speakr-url 都起來了"; break
  fi
  printf '.'; sleep 2
done
curl -fsS -o /dev/null http://127.0.0.1:8897/api/health || { echo; journalctl --user -u speakr-url -n 30 --no-pager; die "speakr-url 沒起來"; }
curl -fsS http://127.0.0.1:8899/static/js/url-import.js | grep -q speakr-url-import-btn || die "Speakr 沒有載到 url-import.js（映像沒換到？）"
podman run --rm --entrypoint grep "$IMAGE" -q speakr-namecheck-btn /app/templates/components/detail/desktop-transcription-panel.html \
  || die "逐字稿工具列沒有「校正姓名」按鈕（patch_speakr.py 沒套到？）"
podman run --rm --entrypoint grep "$IMAGE" -q speakr-tr-panel /app/templates/components/detail/desktop-right-panel.html \
  || die "右側沒有「繁中翻譯」分頁（patch_speakr.py 沒套到？）"
podman run --rm --entrypoint grep "$IMAGE" -q speakr-tr-left-tab /app/templates/components/detail/desktop-transcription-panel.html \
  || die "逐字稿面板沒有「繁中翻譯」分頁（patch_speakr.py 沒套到？）"
podman run --rm --entrypoint grep "$IMAGE" -q speakr-obs-btn /app/templates/components/header.html \
  || die "標題列沒有「匯入 Obsidian」按鈕（patch_speakr.py 沒套到？）"
ok "Speakr 已載入網址匯入欄位、「校正姓名」按鈕、「繁中翻譯」分頁"

echo "== 4/5 tailscale serve：https://…:$TS_PORT/url-import → 127.0.0.1:8897"
if ! tailscale serve --bg --https="$TS_PORT" --set-path=/url-import http://127.0.0.1:8897 2>/dev/null; then
  warn "需要 root（或 sudo tailscale set --operator=$USER）"
  sudo tailscale serve --bg --https="$TS_PORT" --set-path=/url-import http://127.0.0.1:8897
fi
tailscale serve status

echo "== 5/5 從 tailnet 測試"
TS_HOST=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
if curl -fsS "https://$TS_HOST:$TS_PORT/url-import/api/health"; then
  echo; ok "https://$TS_HOST:$TS_PORT/url-import/api/health"
else
  warn "從 tailnet 打不到 /url-import，檢查 tailscale serve status"
fi

echo
echo "完成。打開 https://$TS_HOST:$TS_PORT → 上傳 → 「從網址匯入」貼網址。"
echo "還原：cp $QUADLET_DIR/speakr.container.bak-url $QUADLET_DIR/speakr.container && systemctl --user daemon-reload && systemctl --user restart speakr"

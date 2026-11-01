#!/usr/bin/env bash
# 「從伺服器資料夾選檔」：把主機的 /mnt（唯讀）掛進 speakr-url 容器，上傳視窗就會多出一個檔案選擇面板。
#   選影片或音訊 → 伺服器用 ffmpeg 只抽出音訊（96 kbps 單聲道，2 小時約 85 MB）→ 交給 Speakr 轉錄。
#   大檔（幾 GB 的電影）不經過瀏覽器，也不受 Speakr 的「1000 MB 上傳上限」影響。
#
# 需要先裝好網址匯入（./scripts/install-url-import.sh；新版映像才有這個功能）。
# 可覆寫：LOCAL_DIR=/mnt（要開放瀏覽的資料夾，越小越安全，例如 LOCAL_DIR=/mnt/mybook/video）
#         ALL_USERS=1（預設只有 Speakr 管理員能瀏覽、匯入；設 1 讓所有登入的人都能用）
# 移除：rm ~/.config/containers/systemd/speakr-url.container.d/local-files.conf && systemctl --user daemon-reload && systemctl --user restart speakr-url
set -euo pipefail

DROPIN_DIR="$HOME/.config/containers/systemd/speakr-url.container.d"
LOCAL_DIR=${LOCAL_DIR:-/mnt}
LOCAL_DIR=${LOCAL_DIR%/}

ok()   { printf '  \e[32m✔\e[0m %s\n' "$*"; }
warn() { printf '  \e[33m!\e[0m %s\n' "$*"; }
die()  { printf '  \e[31m✘\e[0m %s\n' "$*"; exit 1; }

echo "== 1/3 檢查 $LOCAL_DIR"
[ -n "$LOCAL_DIR" ] && [ -d "$LOCAL_DIR" ] || die "$LOCAL_DIR 不是資料夾"
case "$LOCAL_DIR" in /etc|/root|/home|/usr|/var|/proc|/sys|/boot|/dev) die "$LOCAL_DIR 不該開放，請指定媒體所在的資料夾（例如 /mnt）";; esac
N=$(find "$LOCAL_DIR" -maxdepth 4 -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.m4a' -o -iname '*.mp3' -o -iname '*.wav' -o -iname '*.mov' -o -iname '*.flac' \) 2>/dev/null | head -200 | wc -l)
ok "$LOCAL_DIR（前幾層找到 $N 個影音檔）"
[ "$N" -gt 0 ] || warn "沒找到影音檔；外接硬碟有掛上嗎？（掛上後不用重跑，重新整理上傳視窗就看得到）"

echo "== 2/3 drop-in"
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_DIR/local-files.conf" <<EOT
# 由 install-local-files.sh 產生：把 $LOCAL_DIR 唯讀掛進來，上傳視窗的「從伺服器資料夾選檔」瀏覽這裡
[Container]
Volume=$LOCAL_DIR:/import:ro
Environment=LOCAL_IMPORT_DIR=/import
Environment=LOCAL_IMPORT_ADMIN_ONLY=$([ "${ALL_USERS:-0}" = 1 ] && echo 0 || echo 1)
EOT
systemctl --user daemon-reload
systemctl --user restart speakr-url.service
ok "$DROPIN_DIR/local-files.conf"

echo "== 3/3 檢查"
for _ in $(seq 1 30); do curl -fsS -o /dev/null http://127.0.0.1:8897/api/health && break; sleep 1; done
H=$(curl -fsS http://127.0.0.1:8897/api/health) || { journalctl --user -u speakr-url -n 30 --no-pager; die "speakr-url 沒起來"; }
echo "  $H"
echo "$H" | grep -Eq '"localfiles": ?true' || die "speakr-url 看不到 /import（映像太舊就先跑 YTDLP_REFRESH=keep ./scripts/install-url-import.sh）"
ok "完成。重新整理 Speakr → 上傳 → 「📁 從伺服器資料夾選檔」"
echo "  容器是用你的帳號讀檔：你讀不到的檔案（權限 600、別人的家目錄）這裡也看不到。"
echo "  原檔不會被改動或刪除；Speakr 只收到抽出來的音訊。"

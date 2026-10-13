#!/usr/bin/env bash
# 「匯入 Obsidian」按鈕：把 vault 裡的一個資料夾（預設 Speakr/）掛進 speakr-url 容器。
# 需要先裝好網址匯入（./scripts/install-url-import.sh，新版映像才有匯出功能）與 obsidian-server。
# 可覆寫：VAULT_DIR=/mnt/mybook/obsidian/vault OBSIDIAN_FOLDER=Speakr TS_PORT=8899
# 移除：rm ~/.config/containers/systemd/speakr-url.container.d/obsidian.conf && systemctl --user daemon-reload && systemctl --user restart speakr-url
set -euo pipefail

DROPIN_DIR="$HOME/.config/containers/systemd/speakr-url.container.d"
FOLDER=${OBSIDIAN_FOLDER:-Speakr}
TS_PORT=${TS_PORT:-8899}

ok()   { printf '  \e[32m✔\e[0m %s\n' "$*"; }
warn() { printf '  \e[33m!\e[0m %s\n' "$*"; }
die()  { printf '  \e[31m✘\e[0m %s\n' "$*"; exit 1; }

echo "== 1/4 找 vault"
if [ -z "${VAULT_DIR:-}" ] && [ -f "$HOME/.config/obsidian-server/env" ]; then
  VAULT_DIR=$(grep -E '^VAULT_DIR=' "$HOME/.config/obsidian-server/env" | tail -1 | cut -d= -f2- | tr -d '"'"'" || true)
  if [ -z "$VAULT_DIR" ]; then
    D=$(grep -E '^DATA_DIR=' "$HOME/.config/obsidian-server/env" | tail -1 | cut -d= -f2- | tr -d '"'"'" || true)
    [ -n "$D" ] && VAULT_DIR="$D/vault"
  fi
fi
VAULT_DIR=${VAULT_DIR:-/mnt/mybook/obsidian/vault}
[ -d "$VAULT_DIR" ] || die "找不到 vault：$VAULT_DIR（外接硬碟有掛上嗎？或用 VAULT_DIR=… 指定）"
[ -d "$VAULT_DIR/.obsidian" ] || warn "$VAULT_DIR 底下沒有 .obsidian/，確定這是 vault 嗎？（Ctrl+C 中止）"
TARGET="$VAULT_DIR/$FOLDER"
mkdir -p "$TARGET"
touch "$TARGET/.speakr-write-test" && rm -f "$TARGET/.speakr-write-test" || die "$TARGET 不能寫入"
ok "匯出到 $TARGET（vault 內：$FOLDER/）"

echo "== 2/4 Speakr 對外網址（筆記裡的「在 Speakr 開啟」連結）"
PUBLIC=""
if command -v tailscale >/dev/null; then
  H=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)
  [ -n "$H" ] && PUBLIC="https://$H:$TS_PORT"
fi
[ -n "$PUBLIC" ] && ok "$PUBLIC" || warn "抓不到 tailnet 網址，筆記裡不放 Speakr 連結"

echo "== 3/4 drop-in"
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_DIR/obsidian.conf" <<EOF
# 由 install-obsidian-export.sh 產生：只把 vault 的 $FOLDER/ 資料夾掛進來（可寫），其他筆記看不到
# 注意：外接硬碟沒掛上時 speakr-url 會起不來（掛好後 systemctl --user restart speakr-url）
[Container]
Volume=$TARGET:/obsidian:rw
Environment=OBSIDIAN_DIR=/obsidian
Environment=OBSIDIAN_FOLDER=$FOLDER
Environment=SPEAKR_PUBLIC_URL=$PUBLIC
EOF
systemctl --user daemon-reload
systemctl --user restart speakr-url.service
ok "$DROPIN_DIR/obsidian.conf"

echo "== 4/4 檢查"
for _ in $(seq 1 30); do curl -fsS -o /dev/null http://127.0.0.1:8897/api/health && break; sleep 1; done
H=$(curl -fsS http://127.0.0.1:8897/api/health) || { journalctl --user -u speakr-url -n 30 --no-pager; die "speakr-url 沒起來"; }
echo "  $H"
echo "$H" | grep -Eq '"obsidian": ?true' || die "speakr-url 看不到 /obsidian（映像太舊就先跑 YTDLP_REFRESH=keep ./scripts/install-url-import.sh）"
ok "完成。打開錄音 → 標題列分享鈕旁邊的 💎「匯入 Obsidian」"
echo "  筆記會出現在 arch 上的 Obsidian（$FOLDER/），LiveSync 幾秒內同步到其他裝置。"

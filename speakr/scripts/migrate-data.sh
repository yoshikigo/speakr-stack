#!/usr/bin/env bash
# 把 Speakr 的資料從 podman volume 搬到一般資料夾（預設 /mnt/expansion/speakr）：
#     speakr-uploads      → /mnt/expansion/speakr/uploads      錄音檔（最吃空間）
#     speakr-instance     → /mnt/expansion/speakr/instance     SQLite 資料庫、設定
#     speakr-translations → /mnt/expansion/speakr/url-data     繁中翻譯、Obsidian 匯出記錄
#
# 流程：停服務 → 複製（不刪舊的）→ 核對檔案數與大小 → 改 Quadlet → 啟動。核對不過就不切換，舊資料完全沒動。
# 舊 volume 會保留，確認一切正常後自己刪（最後會印指令）；要退回去：./scripts/migrate-data.sh --rollback
#
# 用法：
#   ./scripts/migrate-data.sh                 搬到 /mnt/expansion/speakr
#   ./scripts/migrate-data.sh /mnt/xxx        搬到別的資料夾
#   MOVE="uploads url-data" ./scripts/migrate-data.sh   只搬部分（資料庫留在內建硬碟，見下面的檔案系統說明）
#   ./scripts/migrate-data.sh --rollback      退回 podman volume
#
# 檔案系統：/mnt/expansion/speakr 最好是 ext4 / xfs / btrfs。exFAT / NTFS / FAT 做資料庫（SQLite）斷電容易損毀、
# 也不支援權限；遇到時腳本會警告，建議只搬 uploads 和 url-data（MOVE="uploads url-data"）。
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
QUADLET_DIR="$HOME/.config/containers/systemd"
. "$HERE/scripts/lib-data.sh"

ok()   { printf '  \e[32m✔\e[0m %s\n' "$*"; }
warn() { printf '  \e[33m!\e[0m %s\n' "$*"; }
die()  { printf '  \e[31m✘\e[0m %s\n' "$*"; exit 1; }

[ "$(id -u)" -ne 0 ] || die "請用一般使用者執行（rootless podman 的 volume 在你自己的帳號底下）"

# 三組資料：名稱 → volume 名稱 → 容器內路徑
declare -A VOL=( [uploads]=speakr-uploads [instance]=speakr-instance [url-data]=speakr-translations )

restart_services() {
  systemctl --user daemon-reload
  systemctl --user start speakr.service
  systemctl --user start speakr-url.service 2>/dev/null || true
  printf '  等待 Speakr'
  for _ in $(seq 1 60); do
    curl -fsS -o /dev/null http://127.0.0.1:8899/login && { echo; ok "Speakr 起來了"; return 0; }
    printf '.'; sleep 2
  done
  echo; journalctl --user -u speakr -n 30 --no-pager
  die "Speakr 沒有起來（上面是 log）。舊資料還在 volume 裡，./scripts/migrate-data.sh --rollback 可以退回"
}

# ------------------------------------------------------------------ 退回
if [ "${1:-}" = "--rollback" ]; then
  [ -f "$DATA_DIRS_FILE" ] || die "沒有搬過（$DATA_DIRS_FILE 不存在）"
  echo "== 退回 podman volume（資料夾裡之後新增的錄音不會自動搬回 volume！）"
  for n in uploads instance url-data; do
    d=$(_dd "$n"); [ -n "$d" ] || continue
    podman volume exists "${VOL[$n]}" || die "volume ${VOL[$n]} 已經不在了，沒辦法退回（資料在 $d，請手動處理）"
  done
  systemctl --user stop speakr-url.service speakr.service 2>/dev/null || true
  for f in speakr.container speakr-url.container; do
    p="$QUADLET_DIR/$f"
    for n in uploads instance url-data; do
      d=$(_dd "$n"); [ -n "$d" ] || continue
      sed -i "s#^Volume=$d:#Volume=${VOL[$n]}:#" "$p"
    done
    sed -i '/speakr-data-guard/,+1d' "$p"
  done
  mv "$DATA_DIRS_FILE" "$DATA_DIRS_FILE.rolled-back-$(date +%Y%m%d-%H%M)"
  restart_services
  ok "已退回 volume。搬出去的資料夾沒有刪，確認後自己處理。"
  exit 0
fi

TARGET=${1:-/mnt/expansion/speakr}
TARGET=${TARGET%/}
MOVE=${MOVE:-"uploads instance url-data"}
case "$TARGET" in /|/mnt|/home|/root|/usr|/etc|/var) die "目標資料夾 $TARGET 不合適，請指定專用資料夾，例如 /mnt/expansion/speakr";; esac
[ -f "$DATA_DIRS_FILE" ] && die "已經搬過了（$DATA_DIRS_FILE）。要換位置請先 --rollback"
[ -f "$QUADLET_DIR/speakr.container" ] || die "找不到 speakr.container，先跑 ./scripts/install.sh"
for n in $MOVE; do [ -n "${VOL[$n]:-}" ] || die "MOVE 只能是 uploads / instance / url-data（收到：$n）"; done

echo "== 1/6 目標資料夾 $TARGET"
PARENT=$(dirname "$TARGET")
[ -d "$PARENT" ] || die "$PARENT 不存在（硬碟沒掛上？）。不會替你建立上層資料夾，免得資料寫進系統碟的空資料夾"
if [ "$PARENT" != /mnt ] && [ "$PARENT" != / ] && ! mountpoint -q "$PARENT"; then
  warn "$PARENT 不是掛載點：外接硬碟可能沒掛上，現在寫進去會落在系統碟。"
  read -rp "  確定要繼續嗎？[y/N] " a; [ "${a:-n}" = y ] || exit 1
fi
if [ ! -d "$TARGET" ]; then
  warn "$TARGET 不存在，用 sudo 建立並交給 $USER"
  sudo mkdir "$TARGET" && sudo chown "$USER":"$(id -gn)" "$TARGET"
fi
touch "$TARGET/.write-test" 2>/dev/null && rm -f "$TARGET/.write-test" || {
  warn "$TARGET 不能寫入，嘗試 sudo chown"
  sudo chown "$USER":"$(id -gn)" "$TARGET"
  touch "$TARGET/.write-test" && rm -f "$TARGET/.write-test" || die "$TARGET 還是不能寫入"
}
FSTYPE=$(findmnt -no FSTYPE -T "$TARGET" 2>/dev/null || echo unknown)
SRC_MNT=$(findmnt -no TARGET -T "$TARGET" 2>/dev/null || echo "?")
ok "檔案系統 $FSTYPE（掛載點 $SRC_MNT）"
case "$FSTYPE" in
  exfat|vfat|msdos|ntfs|ntfs3|fuseblk|fuse.*|cifs|nfs*|9p)
    case " $MOVE " in *" instance "*)
      warn "$FSTYPE 不適合放 SQLite 資料庫（斷電容易損毀、鎖定行為不可靠）。"
      warn "建議只搬錄音檔：Ctrl+C 中止後執行  MOVE=\"uploads url-data\" $0 $TARGET"
      read -rp "  仍要把資料庫也搬過去嗎？[y/N] " a; [ "${a:-n}" = y ] || exit 1;;
    esac;;
esac

IMAGE=docker.io/learnedmachine/speakr:lite
podman image exists localhost/speakr-url:latest && IMAGE=localhost/speakr-url:latest
podman image exists "$IMAGE" || podman pull "$IMAGE"

echo "== 2/6 容量檢查"
need=0
for n in $MOVE; do
  podman volume exists "${VOL[$n]}" || { warn "volume ${VOL[$n]} 不存在，略過 $n"; continue; }
  mp=$(podman volume inspect -f '{{.Mountpoint}}' "${VOL[$n]}")
  sz=$(podman unshare du -sb "$mp" 2>/dev/null | cut -f1 || echo 0)
  awk -v n="$n" -v s="$sz" 'BEGIN{printf "  %-10s %8.1f MB\n", n, s/1048576}'
  need=$((need + sz))
done
free=$(df -B1 --output=avail "$TARGET" | tail -1 | tr -d ' ')
[ "$free" -gt $((need + need / 10 + 104857600)) ] || die "$TARGET 剩餘空間 $((free / 1048576)) MB，不夠放 $((need / 1048576)) MB"
ok "空間夠（剩 $((free / 1048576)) MB）"

echo "== 3/6 停止服務"
systemctl --user stop speakr-url.service speakr.service 2>/dev/null || true
for _ in $(seq 1 30); do podman ps --format '{{.Names}}' | grep -Eqx 'speakr|speakr-url' || break; sleep 1; done
podman ps --format '{{.Names}}' | grep -Eqx 'speakr|speakr-url' && die "speakr 容器停不下來"
ok "已停止（錄音處理中的工作會在重啟後由 Speakr 自己接續）"

echo "== 4/6 複製（保留擁有者與權限；舊 volume 不動）"
TMPMAP=$(mktemp)
for n in $MOVE; do
  podman volume exists "${VOL[$n]}" || continue
  dst="$TARGET/$n"
  mkdir -p "$dst"
  if [ -n "$(ls -A "$dst" 2>/dev/null)" ]; then
    die "$dst 不是空的。為了不蓋掉東西，請先確認內容（要用現有資料就直接寫 $DATA_DIRS_FILE，見 README）"
  fi
  printf '  %s … ' "$n"
  podman run --rm --security-opt label=disable --entrypoint cp \
      -v "${VOL[$n]}":/from:ro -v "$dst":/to "$IMAGE" -a /from/. /to/
  echo "完成"
  echo "$n=$dst" >> "$TMPMAP"
done

echo "== 5/6 核對（檔案數與總位元組，兩邊都要完全相同）"
for n in $MOVE; do
  podman volume exists "${VOL[$n]}" || continue
  cmp_cmd='cd "$1" && { find . | wc -l; find . -type f -printf "%s\n" | awk "{s+=\$1} END{print s+0}"; }'
  a=$(podman run --rm --security-opt label=disable --entrypoint sh -v "${VOL[$n]}":/v:ro "$IMAGE" -c "$cmp_cmd" _ /v | tr '\n' ' ')
  b=$(podman run --rm --security-opt label=disable --entrypoint sh -v "$TARGET/$n":/v:ro "$IMAGE" -c "$cmp_cmd" _ /v | tr '\n' ' ')
  [ "$a" = "$b" ] || { rm -f "$TMPMAP"; die "$n 核對不符（volume: $a／資料夾: $b）。沒有切換任何設定，服務請手動 systemctl --user start speakr speakr-url"; }
  ok "$n：$a（項目數 / 位元組）"
done

echo "== 6/6 切換 Quadlet 並啟動"
echo "marker=$TARGET/.speakr-data" >> "$TMPMAP"
date -Is > "$TARGET/.speakr-data"
echo "# 這個資料夾是 Speakr 的資料（migrate-data.sh 建立）。marker 檔 .speakr-data 在才允許 Speakr 啟動——外接硬碟沒掛上時不會當成全新安裝。" > "$TARGET/README.txt"
mkdir -p "$(dirname "$DATA_DIRS_FILE")"
mv "$TMPMAP" "$DATA_DIRS_FILE"
chmod 600 "$DATA_DIRS_FILE"
for f in speakr.container speakr-url.container; do
  [ -f "$QUADLET_DIR/$f" ] || continue
  cp -n "$QUADLET_DIR/$f" "$QUADLET_DIR/$f.bak-migrate" || true
  apply_data_dirs "$QUADLET_DIR/$f"
done
grep -H "^Volume=\|speakr-data" "$QUADLET_DIR"/speakr.container "$QUADLET_DIR"/speakr-url.container | sed 's#^#  #'
restart_services

# 確認容器看到的是新資料夾
podman inspect speakr --format '{{range .Mounts}}{{.Source}} → {{.Destination}}{{"\n"}}{{end}}' | sed 's#^#  #'
echo
ok "搬完了。打開 Speakr 確認錄音都在、能播放。"
echo "  舊 volume 還在（佔用的空間沒釋放）。用幾天確定沒問題後再刪："
for n in $MOVE; do echo "    podman volume rm ${VOL[$n]}"; done
echo "  退回：./scripts/migrate-data.sh --rollback"
echo "  備份：停 speakr 後 rsync -a $TARGET/ <備份位置>/（SQLite 在 instance/，一起帶走）"

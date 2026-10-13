#!/usr/bin/env bash
# 從 tar.gz 解壓的舊目錄（~/asr/asr-server、~/speakr）搬到這個 git repo（~/speakr-stack）。
#   1. 檢查已安裝的 Quadlet / drop-in / systemd unit 有沒有指到舊目錄（有的話列出並自動改寫，先備份）
#   2. 比對舊目錄和 repo：列出只有舊目錄才有的檔案（例如你自己加的 cookies、A/B 報告），複製過來
#   3. 舊目錄搬到 ~/.cache/speakr-stack-old/<時間>/（不刪，確認沒問題再自己刪）
# 不會停任何服務：已安裝的 Quadlet 在 ~/.config/containers/systemd，跟原始碼放哪裡無關。
set -euo pipefail
REPO=$(cd "$(dirname "$0")" && pwd)
OLD_ASR=${OLD_ASR:-$HOME/asr/asr-server}
OLD_SPEAKR=${OLD_SPEAKR:-$HOME/speakr}
UNITS=("$HOME/.config/containers/systemd" "$HOME/.config/systemd/user")
BACKUP="$HOME/.cache/speakr-stack-old/$(date +%Y%m%d-%H%M)"

ok()   { printf '  \e[32m✔\e[0m %s\n' "$*"; }
warn() { printf '  \e[33m!\e[0m %s\n' "$*"; }

[ -d "$REPO/asr-server" ] && [ -d "$REPO/speakr" ] || { echo "請在 speakr-stack repo 裡執行"; exit 1; }

echo "== 1/3 已安裝的 unit 有沒有指到舊目錄"
# 只抓「整個路徑元件」：~/speakr/… 要抓到，~/speakr-stack/… 不能誤判
PAT="($HOME|%h)/(asr/asr-server|speakr)(/|\$|[[:space:]])"
hits=$(grep -rlE "$PAT" "${UNITS[@]}" 2>/dev/null | grep -v '\.bak' || true)
if [ -z "$hits" ]; then
  ok "沒有（Quadlet 只用映像、volume 和 ~/.config 底下的設定，不需要改）"
else
  for f in $hits; do
    cp "$f" "$f.bak-migrate"
    sed -i -E "s#($HOME|%h)/asr/asr-server#\1/speakr-stack/asr-server#g; s#($HOME|%h)/speakr(/|\$|[[:space:]])#\1/speakr-stack/speakr\2#g" "$f"
    ok "改寫 $f（備份 $f.bak-migrate）"
    grep -nE "speakr-stack" "$f" | sed 's/^/      /'
  done
  systemctl --user daemon-reload
  warn "上面的 unit 改過了，下次重啟服務時生效（現在要套用：systemctl --user restart <服務>）"
fi

echo "== 2/3 只有舊目錄才有的檔案"
copy_extra() {  # $1=舊目錄 $2=repo 子目錄
  [ -d "$1" ] || { ok "$1 不存在，略過"; return; }
  local n=0
  while IFS= read -r -d '' f; do
    rel=${f#"$1"/}
    case "$rel" in *.pyc|*/__pycache__/*|__pycache__/*|*.tar.gz) continue;; esac
    if [ ! -e "$2/$rel" ]; then
      mkdir -p "$(dirname "$2/$rel")"; cp -p "$f" "$2/$rel"; n=$((n+1))
      echo "      + $2/$rel"
    fi
  done < <(find "$1" -type f -print0)
  ok "$1：$n 個檔案複製進 repo（.gitignore 會擋掉報告、備份檔，要不要 commit 你決定）"
}
copy_extra "$OLD_ASR" "$REPO/asr-server"
copy_extra "$OLD_SPEAKR" "$REPO/speakr"

echo "== 3/3 舊目錄移到備份"
mkdir -p "$BACKUP"
for d in "$OLD_ASR" "$OLD_SPEAKR"; do
  [ -d "$d" ] && mv "$d" "$BACKUP/" && ok "$d → $BACKUP/$(basename "$d")"
done
rmdir "$HOME/asr" 2>/dev/null && ok "移除空的 ~/asr" || true
echo
echo "完成。之後一律在 $REPO 底下跑腳本，更新用 git pull。"
echo "確認都正常後可刪備份：rm -rf $HOME/.cache/speakr-stack-old"

#!/usr/bin/env bash
# 調高 Speakr 的上傳大小上限。上傳視窗出現
#     Video "xxx.mkv" exceeds the video upload maximum of 1000 MB and was skipped.
# 就是這個設定（Speakr 管理員設定 max_audio_only_video_size_mb，預設 = max_file_size_mb × 4 = 1000）。
# 不是 tailscale / 反向代理的限制，所以改這個就夠了。
#
# 用法：
#   ./scripts/set-upload-limit.sh            影片上限 → 8192 MB（8 GB）
#   ./scripts/set-upload-limit.sh 20000      影片上限 → 20000 MB
#   ./scripts/set-upload-limit.sh 8192 500   影片上限 8192 MB；音訊檔上限 500 MB
#   ./scripts/set-upload-limit.sh --show     只看目前的值
#
# 也可以用網頁改：Speakr 右上角使用者選單 → Admin → System Settings → 找 max_audio_only_video_size_mb → Edit。
# 提醒：
#   · 影片只留音訊（預設 VIDEO_RETENTION=false），但上傳過程會先把整支影片存進資料夾再抽音訊，
#     所以上傳資料夾（/mnt/expansion/speakr/uploads）要有影片大小的空位。
#   · 好幾 GB 的檔案，用上傳視窗的「📁 從伺服器資料夾選檔」更好：不經瀏覽器、不存原片。
set -euo pipefail

ok()   { printf '  \e[32m✔\e[0m %s\n' "$*"; }
die()  { printf '  \e[31m✘\e[0m %s\n' "$*"; exit 1; }

SHOW=0; [ "${1:-}" = "--show" ] && SHOW=1 && shift
VIDEO_MB=${1:-8192}
AUDIO_MB=${2:-}
[[ "$VIDEO_MB" =~ ^[0-9]+$ ]] && [ "$VIDEO_MB" -ge 100 ] && [ "$VIDEO_MB" -le 204800 ] || die "影片上限請填 100–204800（MB），收到：$VIDEO_MB"
[ -z "$AUDIO_MB" ] || { [[ "$AUDIO_MB" =~ ^[0-9]+$ ]] && [ "$AUDIO_MB" -ge 10 ] && [ "$AUDIO_MB" -le 204800 ]; } || die "音訊上限請填 10–204800（MB），收到：$AUDIO_MB"
podman ps --format '{{.Names}}' | grep -qx speakr || die "speakr 容器沒在跑（systemctl --user start speakr）"

podman exec -i speakr python - "$SHOW" "$VIDEO_MB" "$AUDIO_MB" <<'PY'
import os, sqlite3, sys
show, video, audio = sys.argv[1] == "1", sys.argv[2], sys.argv[3]
uri = os.environ.get("SQLALCHEMY_DATABASE_URI", "sqlite:////data/instance/transcriptions.db")
path = uri.split("sqlite:///", 1)[1] if "sqlite:///" in uri else ""
if not path or not os.path.exists(path):
    sys.exit(f"找不到資料庫（{uri}）")
db = sqlite3.connect(path, timeout=30)
if not db.execute("select 1 from sqlite_master where name='system_setting'").fetchone():
    sys.exit("資料庫裡沒有 system_setting 表（Speakr 版本太舊或太新？）")

def get(k):
    r = db.execute("select value from system_setting where key=?", (k,)).fetchone()
    return r[0] if r else None

def put(k, v, desc):
    if db.execute("update system_setting set value=?, setting_type='integer' where key=?", (v, k)).rowcount == 0:
        db.execute("insert into system_setting (key, value, description, setting_type) values (?,?,?,'integer')", (k, v, desc))

print(f"  目前：max_file_size_mb={get('max_file_size_mb') or '250（預設）'}  "
      f"max_audio_only_video_size_mb={get('max_audio_only_video_size_mb') or '未設定（= 音訊上限 × 4）'}")
if not show:
    put("max_audio_only_video_size_mb", video, "Maximum file size for video uploads in audio-only mode (MB).")
    if audio:
        put("max_file_size_mb", audio, "Maximum file size allowed for audio uploads in megabytes (MB).")
    db.commit()
    print(f"  已寫入：max_audio_only_video_size_mb={get('max_audio_only_video_size_mb')}  max_file_size_mb={get('max_file_size_mb') or '250（預設）'}")
PY

if [ "$SHOW" = 0 ]; then
  echo "  重啟 Speakr 讓新上限生效（Werkzeug 的請求大小上限啟動時才讀）…"
  systemctl --user restart speakr.service
  for _ in $(seq 1 60); do curl -fsS -o /dev/null http://127.0.0.1:8899/login && break; sleep 2; done
  curl -fsS -o /dev/null http://127.0.0.1:8899/login || die "Speakr 沒有起來（journalctl --user -u speakr -n 50）"
  journalctl --user -u speakr --since "-2min" --no-pager 2>/dev/null | grep -o "Set MAX_CONTENT_LENGTH to [^(]*" | tail -1 | sed 's/^/  /' || true
  ok "完成。重新整理網頁再上傳（舊分頁的上限是頁面載入時讀的，要 F5）。"
fi

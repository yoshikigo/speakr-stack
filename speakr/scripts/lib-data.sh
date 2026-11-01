# 由 install*.sh / migrate-data.sh 引入（source），不要單獨執行。
#
# 資料放哪裡：預設是 podman volume（speakr-uploads / speakr-instance / speakr-translations）。
# 跑過 ./scripts/migrate-data.sh 之後，~/.config/speakr/data-dirs 會記下搬去的資料夾，內容像這樣：
#     uploads=/mnt/expansion/speakr/uploads
#     instance=/mnt/expansion/speakr/instance
#     url-data=/mnt/expansion/speakr/url-data
#     marker=/mnt/expansion/speakr/.speakr-data
# 之後每次重新安裝／升級複製 Quadlet 時呼叫 apply_data_dirs，就會自動換成這些路徑，不會被範本改回 volume。

DATA_DIRS_FILE="${DATA_DIRS_FILE:-$HOME/.config/speakr/data-dirs}"

_dd() { [ -f "$DATA_DIRS_FILE" ] && sed -n "s/^$1=//p" "$DATA_DIRS_FILE" | head -1 || true; }

# apply_data_dirs <Quadlet 檔>：把 Volume=speakr-xxx:… 換成 bind mount；有 marker 就加上「沒掛載就不啟動」檢查
apply_data_dirs() {
  local f=$1 u i t m
  [ -f "$DATA_DIRS_FILE" ] && [ -f "$f" ] || return 0
  u=$(_dd uploads); i=$(_dd instance); t=$(_dd url-data); m=$(_dd marker)
  [ -n "$u" ] && sed -i "s#^Volume=speakr-uploads:#Volume=$u:#" "$f"
  [ -n "$i" ] && sed -i "s#^Volume=speakr-instance:#Volume=$i:#" "$f"
  [ -n "$t" ] && sed -i "s#^Volume=speakr-translations:#Volume=$t:#" "$f"
  # 外接硬碟沒掛上時，/mnt/expansion/speakr 只是空資料夾：照常啟動會被當成全新安裝（空資料庫、重建管理員）。
  # 所以啟動前先確認 marker 檔在；不在就啟動失敗，每 10 秒重試，硬碟掛上後自動恢復。
  if [ -n "$m" ] && ! grep -q "speakr-data-guard" "$f"; then
    sed -i "s|^\[Service\]|[Service]\n# speakr-data-guard：資料在 $(dirname "$m")，沒掛載就不要啟動\nExecStartPre=/usr/bin/test -f $m|" "$f"
  fi
}

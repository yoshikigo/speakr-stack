#!/usr/bin/env bash
# 加入 Breeze-ASR-26（台語／台華混合語音 → 中文字，Whisper large-v2 微調，Apache 2.0）當第三個模型。
#   1. 下載官方 MediaTek-Research/Breeze-ASR-26 並轉成 CTranslate2 → volume asr-models:/models/breeze-asr-26
#   2. asr-server Quadlet 的 ASR_MODELS 加上 breeze-asr-26，重啟
#   3. 冒煙測試
# 之後在 Speakr 那邊跑 ~/speakr/scripts/use-asr-diarize.sh，選單就會多出「A3：Breeze-ASR-26 台語」。
# 可重複執行；FORCE=1 重新轉檔；BREEZE26_REVISION=<commit> 釘版本。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
VOL=${VOL:-asr-models}
UNIT="$HOME/.config/containers/systemd/asr-server.container"

ok()  { printf '  \e[32m✔\e[0m %s\n' "$*"; }
die() { printf '  \e[31m✘\e[0m %s\n' "$*"; exit 1; }

[ -f "$UNIT" ] || die "找不到 $UNIT（先裝 asr-server）"
podman volume exists "$VOL" || die "沒有 volume $VOL"

echo "== 1/3 下載 + 轉檔（約 6 GB 權重，CPU 轉檔 5–10 分鐘）"
FORCE_ARG=""; [ "${FORCE:-0}" = 1 ] && FORCE_ARG="--force"
podman run --rm -v "$VOL":/models -v "$HERE/convert_breeze.py":/convert_breeze.py:ro \
    -e HF_HOME=/tmp/hf -e BREEZE_REPO=MediaTek-Research/Breeze-ASR-26 \
    -e BREEZE_OUT=/models/breeze-asr-26 -e BREEZE_REVISION="${BREEZE26_REVISION:-}" \
    docker.io/library/python:3.12-slim sh -euc "
        pip install -q --root-user-action=ignore --extra-index-url https://download.pytorch.org/whl/cpu \
            ctranslate2==4.8.2 'transformers>=4.45,<6' torch huggingface_hub
        python /convert_breeze.py $FORCE_ARG
    "
ok "/models/breeze-asr-26"

echo "== 2/3 asr-server 加入模型"
cp "$UNIT" "$UNIT.bak-$(date +%Y%m%d-%H%M)"
if grep -q '^Environment=ASR_MODELS=.*breeze-asr-26=' "$UNIT"; then
  ok "ASR_MODELS 已經有 breeze-asr-26"
else
  sed -i 's#^\(Environment=ASR_MODELS=.*\)$#\1,breeze-asr-26=/models/breeze-asr-26#' "$UNIT"
  grep -q 'breeze-asr-26=/models/breeze-asr-26' "$UNIT" || die "改不到 ASR_MODELS（看一下 $UNIT）"
  ok "$(grep '^Environment=ASR_MODELS=' "$UNIT")"
fi
systemctl --user daemon-reload
systemctl --user restart asr-server.service
for _ in $(seq 1 30); do curl -fsS -o /dev/null http://127.0.0.1:8098/health && break; sleep 2; done
curl -fsS http://127.0.0.1:8098/health | grep -q 'breeze-asr-26' || { journalctl --user -u asr-server -n 30 --no-pager; die "asr-server 沒載到 breeze-asr-26"; }
ok "asr-server 有 breeze-asr-26"

echo "== 3/3 冒煙測試（第一次會載入模型，約 10 秒）"
if command -v espeak-ng >/dev/null; then
  TMP=$(mktemp -d)
  espeak-ng -v cmn "大家好，今天天氣很好。" -w "$TMP/a.wav"
  curl -fsS -F "file=@$TMP/a.wav" -F model=breeze-asr-26 http://127.0.0.1:8098/v1/audio/transcriptions && echo
  rm -rf "$TMP"
  ok "能轉錄（合成語音是華語，只測能不能跑；台語要用真的錄音測）"
fi
echo
echo "下一步：cd ~/speakr && ./scripts/use-asr-diarize.sh   # Speakr 選單加入 A3"
echo "VRAM：ASR_MAX_LOADED=1 時同一時間只常駐一個模型，切換模型會自動卸載前一個，不會多吃 VRAM。"

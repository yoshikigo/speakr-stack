#!/usr/bin/env bash
# 升級已部署的 asr-server：加上 /asr（sherpa-onnx 說話者分辨 + 轉送 WhisperX 做 A/B）。
# 模型 volume 不動；舊 Quadlet 備份成 asr-server.container.bak-<時間>。
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
UNIT="$HOME/.config/containers/systemd/asr-server.container"

ok()  { printf '  \e[32m✔\e[0m %s\n' "$*"; }
die() { printf '  \e[31m✘\e[0m %s\n' "$*"; exit 1; }

echo "== 1/4 build（大層會用快取；新增 sherpa-onnx 約 30 MB + 分辨模型 35 MB）"
podman build -t localhost/asr-server:latest "$HERE"

echo "== 2/4 Quadlet（改成 host 網路、綁 127.0.0.1:8098，才能連到 WhisperX）"
[ -f "$UNIT" ] && cp "$UNIT" "$UNIT.bak-$(date +%Y%m%d-%H%M)"
cp "$HERE/quadlet/asr-server.container" "$UNIT"
if ! systemctl --user cat whisperx.service >/dev/null 2>&1; then
  sed -i 's/^Environment=ASR_WHISPERX_URL=/# Environment=ASR_WHISPERX_URL=/' "$UNIT"
  ok "沒裝 WhisperX：只啟用 A（sherpa-onnx），不轉送"
fi
systemctl --user daemon-reload
systemctl --user restart asr-server.service

echo "== 3/4 等待服務"
for _ in $(seq 1 30); do curl -fsS -o /dev/null http://127.0.0.1:8098/health && break; sleep 2; done
H=$(curl -fsS http://127.0.0.1:8098/health) || { journalctl --user -u asr-server -n 30 --no-pager; die "asr-server 沒起來"; }
echo "  $H"
echo "$H" | grep -Eq '"diarization": ?true' || die "分辨模型沒載到"
ok "asr-server 已升級"

echo "== 4/4 冒煙測試（/v1 給 LINE、Open WebUI；/asr 給 Speakr）"
TMP=$(mktemp -d)
if command -v espeak-ng >/dev/null; then
  espeak-ng -v cmn "大家好，今天我們來討論下一季的預算。" -w "$TMP/a.wav"
  curl -fsS -F "file=@$TMP/a.wav" http://127.0.0.1:8098/v1/audio/transcriptions && echo
  curl -fsS -F "audio_file=@$TMP/a.wav" "http://127.0.0.1:8098/asr?output=json&diarize=true" | head -c 400 && echo
  ok "兩個端點都正常"
fi
rm -rf "$TMP"

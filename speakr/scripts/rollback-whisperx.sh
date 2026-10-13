#!/usr/bin/env bash
# Speakr 改回直接用 asr-server（無說話者分辨），停掉 WhisperX。資料與 HF token 保留。
set -euo pipefail
SPEAKR_ENV="$HOME/.config/speakr/speakr.env"
[ -f "$SPEAKR_ENV.bak-whisperx" ] || { echo "找不到 $SPEAKR_ENV.bak-whisperx"; exit 1; }
cp "$SPEAKR_ENV.bak-whisperx" "$SPEAKR_ENV"
systemctl --user restart speakr.service
systemctl --user stop whisperx.service || true
echo "已還原。要完全移除：rm ~/.config/containers/systemd/whisperx.container && systemctl --user daemon-reload"
echo "（模型快取 volume whisperx-cache 和 secret hf-token 都還在，需要時再手動刪）"

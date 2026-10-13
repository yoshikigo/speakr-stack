#!/usr/bin/env bash
# 讓 Speakr 改用 WhisperX（說話者分辨 + 聲紋），轉錄仍由 asr-server 的 Breeze-ASR-25 負責。
#
#   Speakr ──/asr──> whisperx (127.0.0.1:9010) ──/v1/audio/transcriptions──> asr-server (Breeze)
#                      └─ Qwen3 對齊 + pyannote 分辨 + 聲紋 embedding（本機 GPU）
#
# 可重複執行。還原：./scripts/rollback-whisperx.sh
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
QUADLET_DIR="$HOME/.config/containers/systemd"
WX_CONF="$HOME/.config/whisperx"
SPEAKR_ENV="$HOME/.config/speakr/speakr.env"
IMAGE=docker.io/learnedmachine/whisperx-asr-service:0.4.2
PORT=9010
GATED=(pyannote/speaker-diarization-community-1 pyannote/segmentation-3.0 pyannote/speaker-diarization-3.1)

ok()   { printf '  \e[32m✔\e[0m %s\n' "$*"; }
warn() { printf '  \e[33m!\e[0m %s\n' "$*"; }
die()  { printf '  \e[31m✘\e[0m %s\n' "$*"; exit 1; }

# 設定 KEY=VALUE（存在就改、不存在就加）；comment_out 會把 KEY= 那行註解掉
set_kv() { python3 - "$1" "$2" "$3" <<'PY'
import re, sys
path, key, val = sys.argv[1:]
lines = open(path, encoding="utf-8").read().splitlines()
pat = re.compile(rf"^#?\s*{re.escape(key)}=")
done = False
for i, l in enumerate(lines):
    if pat.match(l):
        lines[i] = f"{key}={val}"; done = True; break
if not done:
    lines.append(f"{key}={val}")
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
}
comment_out() { sed -i "s/^$2=/# (whisperx 停用) $2=/" "$1"; }

echo "== 1/7 前置檢查"
curl -fsS http://127.0.0.1:8098/health >/dev/null || die "asr-server 沒在跑"
[ -f "$SPEAKR_ENV" ] || die "找不到 $SPEAKR_ENV（先部署 Speakr）"
nvidia-ctk cdi list 2>/dev/null | grep -q 'nvidia.com/gpu=all' || die "沒有 CDI 的 nvidia.com/gpu=all（sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml）"
if ss -ltn | grep -q "127.0.0.1:$PORT \|0.0.0.0:$PORT \|\*:$PORT " && ! podman ps --format '{{.Names}}' | grep -qx whisperx; then
  die "port $PORT 已被占用"
fi
ok "asr-server、Speakr、GPU、port $PORT"

echo "== 2/7 Hugging Face token（pyannote 分辨模型需要）"
if podman secret exists hf-token 2>/dev/null; then
  ok "podman secret hf-token 已存在"
  TOKEN=$(podman secret inspect --showsecret --format '{{.SecretData}}' hf-token 2>/dev/null || true)
else
  cat <<'TXT'
  需要先在 Hugging Face 完成（免費，約 2 分鐘）：
    1. 登入 https://huggingface.co
    2. 打開下面三個頁面，各按一次「Agree and access repository」：
       https://huggingface.co/pyannote/speaker-diarization-community-1
       https://huggingface.co/pyannote/segmentation-3.0
       https://huggingface.co/pyannote/speaker-diarization-3.1
    3. https://huggingface.co/settings/tokens → New token → 權限選 Read
TXT
  read -rsp "  貼上 token（hf_ 開頭）: " TOKEN; echo
  [[ "$TOKEN" == hf_* ]] || die "格式不對（要 hf_ 開頭）"
  printf '%s' "$TOKEN" | podman secret create hf-token - >/dev/null
  ok "已存成 podman secret hf-token"
fi
if [ -n "${TOKEN:-}" ]; then
  for repo in "${GATED[@]}"; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --head -H "Authorization: Bearer $TOKEN" \
      "https://huggingface.co/$repo/resolve/main/config.yaml" || echo 000)
    case "$code" in
      200|302|307) ok "$repo 可存取" ;;
      401) die "token 無效（podman secret rm hf-token 後重跑）" ;;
      403) die "$repo 還沒按同意：打開 https://huggingface.co/$repo 按 Agree" ;;
      *)   warn "$repo 檢查回應 HTTP $code（網路問題？先繼續）" ;;
    esac
  done
fi
unset TOKEN

echo "== 3/7 asr-server：放寬上傳上限（WhisperX 會把整段錄音轉成 WAV 一次送，1 小時約 115 MB）"
ASR_UNIT="$QUADLET_DIR/asr-server.container"
if grep -q '^Environment=ASR_MAX_UPLOAD_MB=' "$ASR_UNIT"; then
  sed -i 's/^Environment=ASR_MAX_UPLOAD_MB=.*/Environment=ASR_MAX_UPLOAD_MB=1024/' "$ASR_UNIT"
else
  sed -i '/^Environment=ASR_OPENCC=/a Environment=ASR_MAX_UPLOAD_MB=1024' "$ASR_UNIT"
fi
grep -q '^Environment=ASR_MAX_UPLOAD_MB=1024' "$ASR_UNIT" || die "沒能改到 $ASR_UNIT"
systemctl --user daemon-reload
systemctl --user restart asr-server.service
for _ in $(seq 1 30); do curl -fsS -o /dev/null http://127.0.0.1:8098/health && break; sleep 2; done
ok "ASR_MAX_UPLOAD_MB=1024"

echo "== 4/7 拉 WhisperX 映像（PyTorch + CUDA，好幾 GB；網路慢的話會很久，可以中斷後重跑）"
podman pull "$IMAGE"
podman run --rm --entrypoint python3 -w /workspace "$IMAGE" -c "import app.external_backend" \
  || die "這個映像沒有 external backend，版本不對"
ok "映像就緒（含 external backend）"

echo "== 5/7 設定 + Quadlet"
mkdir -p "$WX_CONF"
if [ -f "$WX_CONF/whisperx.env" ]; then
  warn "$WX_CONF/whisperx.env 已存在，不覆蓋"
else
  cp "$HERE/whisperx/whisperx.env" "$WX_CONF/whisperx.env"; chmod 600 "$WX_CONF/whisperx.env"
  ok "已建立 $WX_CONF/whisperx.env"
fi
cp "$HERE/whisperx/whisperx.container" "$QUADLET_DIR/"
systemctl --user daemon-reload
systemctl --user restart whisperx.service
printf '  等待 WhisperX 起來'
for _ in $(seq 1 120); do
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/health" && { echo; ok "http://127.0.0.1:$PORT"; break; }
  printf '.'; sleep 5
done
curl -fsS -o /dev/null "http://127.0.0.1:$PORT/health" || { echo; journalctl --user -u whisperx -n 40 --no-pager; die "WhisperX 沒起來"; }

echo "== 6/7 冒煙測試（第一次會下載 pyannote + Qwen 對齊模型，約 1.5 GB）"
TMPW=$(mktemp --suffix=.wav)
if command -v espeak-ng >/dev/null; then
  espeak-ng -v cmn "大家好，今天我們來討論下一季的預算。" -w "$TMPW.a"
  espeak-ng -v cmn+f3 -p 70 "好的，我先報告目前的進度，大概完成八成。" -w "$TMPW.b"
  python3 - "$TMPW.a" "$TMPW.b" "$TMPW" <<'PY'
import sys, wave
out = wave.open(sys.argv[3], "wb")
for i, f in enumerate(sys.argv[1:3]):
    w = wave.open(f)
    if i == 0: out.setparams(w.getparams())
    out.writeframes(w.readframes(w.getnframes()))
    out.writeframes(b"\0\0" * w.getframerate())  # 1 秒靜音
out.close()
PY
  rm -f "$TMPW.a" "$TMPW.b"
  curl -sS --max-time 1800 -F "audio_file=@$TMPW" \
    "http://127.0.0.1:$PORT/asr?output=json&language=zh&diarize=true&min_speakers=1&max_speakers=3" \
    | python3 -c '
import json, sys
d = json.load(sys.stdin)
if "segments" not in d: print("  回應：", d); sys.exit(1)
for s in d["segments"]:
    spk, a, b, txt = s.get("speaker", "?"), s["start"], s["end"], s["text"].strip()
    print(f"  [{spk}] {a:6.1f}-{b:6.1f}s  {txt}")
' && ok "WhisperX → asr-server → 分辨 都通了（機器語音的分辨結果不代表真人準確度）" \
    || warn "測試沒過，看 journalctl --user -u whisperx -n 50"
else
  warn "沒有 espeak-ng，略過冒煙測試"
fi
rm -f "$TMPW"

echo "== 7/7 Speakr 改用 WhisperX"
[ -f "$SPEAKR_ENV.bak-whisperx" ] || cp "$SPEAKR_ENV" "$SPEAKR_ENV.bak-whisperx"
set_kv "$SPEAKR_ENV" TRANSCRIPTION_CONNECTOR asr_endpoint
set_kv "$SPEAKR_ENV" ASR_BASE_URL "http://127.0.0.1:$PORT"
set_kv "$SPEAKR_ENV" ASR_DIARIZE true
set_kv "$SPEAKR_ENV" ASR_RETURN_SPEAKER_EMBEDDINGS true
set_kv "$SPEAKR_ENV" ASR_TIMEOUT 3600
# Speakr 會把 TRANSCRIPTION_MODEL / 下拉選單的名字當 Whisper 模型名送給 WhisperX → 400，必須拿掉
comment_out "$SPEAKR_ENV" TRANSCRIPTION_MODEL
comment_out "$SPEAKR_ENV" TRANSCRIPTION_MODELS_AVAILABLE
systemctl --user restart speakr.service
for _ in $(seq 1 60); do curl -fsS -o /dev/null http://127.0.0.1:8899/login && break; sleep 2; done
ok "Speakr 已切到 asr_endpoint → WhisperX（舊設定備份在 $SPEAKR_ENV.bak-whisperx）"

echo
echo "完成。之後上傳的錄音會標 SPEAKER_00 / SPEAKER_01…，在 Speakr 裡把標籤改成真名；"
echo "同一個人在之後的錄音會靠聲紋自動認出（每人需要約 15 秒以上的發言）。"

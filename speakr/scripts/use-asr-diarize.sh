#!/usr/bin/env bash
# Speakr 改用 asr-server 的 /asr（A：Breeze + sherpa-onnx 說話者分辨），並在模型選單提供 B（WhisperX）做 A/B。
#   上傳 / 重新轉錄時的模型選單：
#     breeze-asr-25     A：Breeze + sherpa 分辨（預設）
#     whisperx          B：WhisperX 分辨（asr-server 轉送，轉錄一樣是 Breeze）
#     whisper-large-v3  A2：Whisper large-v3 + sherpa 分辨（英文多、要標點時）
#     breeze-asr-26     A3：Breeze-ASR-26 台語 + sherpa 分辨（asr-server 跑過 add-breeze26.sh 才會出現）
# 需要 asr-server 已用 upgrade.sh 升級。可重複執行。還原：cp ~/.config/speakr/speakr.env.bak-diarize ~/.config/speakr/speakr.env
set -euo pipefail
ENV="$HOME/.config/speakr/speakr.env"

ok()  { printf '  \e[32m✔\e[0m %s\n' "$*"; }
die() { printf '  \e[31m✘\e[0m %s\n' "$*"; exit 1; }

H=$(curl -fsS http://127.0.0.1:8098/health) || die "asr-server 沒在跑"
echo "$H" | grep -Eq '"diarization": ?true' || die "asr-server 還沒升級（跑 asr-server 的 ./scripts/upgrade.sh）"
HAS_B26=0; echo "$H" | grep -q '"breeze-asr-26"' && HAS_B26=1
HAS_WX=0; echo "$H" | grep -Eq '"whisperx": ?"http' && curl -fsS -o /dev/null http://127.0.0.1:9010/health 2>/dev/null && HAS_WX=1

[ "$HAS_WX" = 1 ] || echo "  （WhisperX 沒在跑或 asr-server 沒設 ASR_WHISPERX_URL → 選單不含 B；要 B 就先 systemctl --user start whisperx 再重跑）"
[ "$HAS_B26" = 1 ] || echo "  （asr-server 還沒有 breeze-asr-26 → 選單不含 A3；先跑 ~/speakr-stack/asr-server/scripts/add-breeze26.sh）"
[ -f "$ENV.bak-diarize" ] || cp "$ENV" "$ENV.bak-diarize"
python3 - "$ENV" "$HAS_WX" "$HAS_B26" <<'PY'
import re, sys
path, has_wx, has_b26 = sys.argv[1], sys.argv[2] == "1", sys.argv[3] == "1"
models = (["breeze-asr-25"] + (["whisperx"] if has_wx else []) + ["whisper-large-v3"]
          + (["breeze-asr-26"] if has_b26 else []))
# 標籤裡不能有半形逗號（Speakr 用逗號分隔），用「・」「、」
labels = (["A：台灣華語・中英夾雜（中文）｜Breeze-ASR-25（預設）"]
          + (["B：同 A 但用 WhisperX 分辨說話者（比較用）"] if has_wx else [])
          + ["A2：英文・日文等外語（自動偵測語言）｜Whisper large-v3"]
          + (["A3：台語・台華夾雜（輸出中文字）｜Breeze-ASR-26"] if has_b26 else []))
want = {
    "TRANSCRIPTION_CONNECTOR": "asr_endpoint",
    "ASR_BASE_URL": "http://127.0.0.1:8098",
    "ASR_DIARIZE": "true",
    "ASR_TIMEOUT": "3600",
    # B（WhisperX）會回傳聲紋，供 Speakr 的聲紋檔案使用；A 不回傳也沒關係
    "ASR_RETURN_SPEAKER_EMBEDDINGS": "true" if has_wx else "false",
    # Speakr 啟動時會拿預設模型（A）檢查聲紋模型是否一致，A 沒有聲紋 → 關掉這個檢查
    "DISABLE_VOICE_EMBEDDING_CHECK": "true",
    "TRANSCRIPTION_MODELS_AVAILABLE": ",".join(models),
    "TRANSCRIPTION_MODEL_LABELS": ",".join(labels),
}
lines = open(path, encoding="utf-8").read().splitlines()
out, seen = [], set()
for l in lines:
    m = re.match(r"^(?:#\s*(?:\(whisperx 停用\)\s*)?)?([A-Z_]+)=", l)
    key = m.group(1) if m else None
    if key == "TRANSCRIPTION_MODEL" and not l.startswith("#"):
        out.append("# (asr diarize) " + l)          # 不要固定模型名，讓選單/預設決定
        continue
    if key in want:
        if key not in seen:
            out.append(f"{key}={want[key]}")
            seen.add(key)
        continue
    out.append(l)
out += [f"{k}={v}" for k, v in want.items() if k not in seen]
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
print("  模型選單：" + "、".join(f"{m}（{l}）" for m, l in zip(models, labels)))
PY
systemctl --user restart speakr.service
for _ in $(seq 1 60); do curl -fsS -o /dev/null http://127.0.0.1:8899/login && break; sleep 2; done
ok "Speakr → asr-server /asr（舊設定：$ENV.bak-diarize）"

if [ "$HAS_WX" = 1 ] && grep -q '^DEVICE=cuda' "$HOME/.config/whisperx/whisperx.env" 2>/dev/null; then
  echo
  read -rp "  WhisperX 只拿來比較，改用 CPU 讓出約 2 GB VRAM 給 Ollama？[Y/n] " yn
  if [ "${yn:-Y}" != n ] && [ "${yn:-Y}" != N ]; then
    sed -i 's/^DEVICE=cuda/DEVICE=cpu/; s/^COMPUTE_TYPE=float16/COMPUTE_TYPE=float32/' "$HOME/.config/whisperx/whisperx.env"
    systemctl --user restart whisperx.service
    ok "WhisperX 改用 CPU（轉錄仍由 asr-server 在 GPU 上做）"
  fi
fi

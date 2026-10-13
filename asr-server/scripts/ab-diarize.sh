#!/usr/bin/env bash
# A/B 比較說話者分辨：同一個音訊檔分別送
#   A：asr-server 本機轉錄 + sherpa-onnx 分辨
#   B：WhisperX（經 asr-server 轉送，轉錄同樣用 Breeze，只有對齊/分辨不同）
# 用法：./scripts/ab-diarize.sh 錄音.m4a [人數]       例：./scripts/ab-diarize.sh talk.m4a 4
# 結果放在 ab-<檔名>-<時間>/：a.json b.json a.txt b.txt report.md
set -euo pipefail

FILE=${1:?用法：$0 錄音檔 [說話人數]}
SPK=${2:-}
ASR=${ASR_URL:-http://127.0.0.1:8098}
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="ab-$(basename "${FILE%.*}" | tr -c '[:alnum:]_.-' '_' | cut -c1-40)-$(date +%m%d-%H%M)"
mkdir -p "$OUT"

Q="output=json&task=transcribe&diarize=true&enable_diarization=true&language=zh"
[ -n "$SPK" ] && Q="$Q&min_speakers=$SPK&max_speakers=$SPK"

curl -fsS "$ASR/health" | grep -Eq '"whisperx": ?"http' \
  || { echo "asr-server 沒有設定 ASR_WHISPERX_URL（或 asr-server 沒在跑）"; exit 1; }

run() {  # $1=a|b  $2=model 參數
  local t0 t1
  echo "== $1：送出 $(basename "$FILE")（$( [ "$1" = a ] && echo 'asr-server + sherpa-onnx' || echo 'WhisperX')）…"
  t0=$(date +%s.%N)
  curl -fsS --max-time 7200 -o "$OUT/$1.json" -F "audio_file=@$FILE" "$ASR/asr?$Q$2" \
    || { echo "   失敗，回應："; cat "$OUT/$1.json" 2>/dev/null; exit 1; }
  t1=$(date +%s.%N)
  python3 -c "print(f'   完成，{($t1-$t0):.0f} 秒')"
  python3 - "$OUT/$1.json" > "$OUT/$1.txt" <<'PY'
import json, sys
for s in json.load(open(sys.argv[1]))["segments"]:
    t = int(s["start"]); print(f"[{t//60:02d}:{t%60:02d}] {s.get('speaker','?')}: {s['text'].strip()}")
PY
  echo "$t0 $t1" > "$OUT/$1.time"
}

run a ""
run b "&model=whisperx"

TA=$(awk '{printf "%.0f", $2-$1}' "$OUT/a.time"); TB=$(awk '{printf "%.0f", $2-$1}' "$OUT/b.time")
python3 "$HERE/ab_compare.py" "$OUT/a.json" "$OUT/b.json" --time-a "$TA" --time-b "$TB" > "$OUT/report.md"
rm -f "$OUT"/*.time
echo
cat "$OUT/report.md"
echo
echo "完整逐字稿：$OUT/a.txt、$OUT/b.txt（可用 diff -y 或 vimdiff 並排看）"

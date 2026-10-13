#!/usr/bin/env bash
# 把兩個模型放進 podman volume「asr-models」：
#   /models/whisper-large-v3   ← Systran/faster-whisper-large-v3（已是 CTranslate2 格式）
#   /models/breeze-asr-25      ← 官方 MediaTek-Research/Breeze-ASR-25 自己轉 CTranslate2
# 可重複執行；已存在的會略過（FORCE=1 強制重做）。
set -euo pipefail

IMAGE=${IMAGE:-localhost/asr-server:latest}
VOL=${VOL:-asr-models}
HERE=$(cd "$(dirname "$0")" && pwd)
FORCE=${FORCE:-0}

podman volume exists "$VOL" || podman volume create "$VOL"

echo "== 1/2 whisper-large-v3 =="
podman run --rm -i --user 0 -e HF_HUB_OFFLINE=0 -e FORCE="$FORCE" -v "$VOL":/models "$IMAGE" python - <<'PY'
import os
from faster_whisper import download_model
out = "/models/whisper-large-v3"
if os.path.exists(f"{out}/model.bin") and os.environ.get("FORCE") != "1":
    print(out, "已存在，略過")
else:
    print(download_model("large-v3", output_dir=out))
PY

echo "== 2/2 Breeze-ASR-25（下載 ~6 GB transformers 權重 + CPU 轉檔，約 5–10 分鐘）=="
FORCE_ARG=""; [ "$FORCE" = 1 ] && FORCE_ARG="--force"
podman run --rm -v "$VOL":/models -v "$HERE/convert_breeze.py":/convert_breeze.py:ro \
    -e HF_HOME=/tmp/hf -e BREEZE_REVISION="${BREEZE_REVISION:-}" \
    docker.io/library/python:3.12-slim sh -euc "
        pip install -q --root-user-action=ignore --extra-index-url https://download.pytorch.org/whl/cpu \
            ctranslate2==4.8.2 'transformers>=4.45,<6' torch huggingface_hub
        python /convert_breeze.py $FORCE_ARG
    "

echo "== 完成 =="
podman run --rm -v "$VOL":/models:ro docker.io/library/python:3.12-slim sh -c 'du -sh /models/*; ls /models/*'

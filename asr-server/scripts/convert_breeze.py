"""把官方 MediaTek-Research/Breeze-ASR-25（transformers 格式）轉成 CTranslate2，給 faster-whisper 用。

在一次性的容器裡跑（見 fetch-models.sh），需要 ctranslate2 + transformers + torch(CPU)。
權重以 float16 存；執行時再由 ASR_COMPUTE_TYPE 決定 int8_float16 / float16。
"""
import os
import shutil
import sys

from ctranslate2.converters import TransformersConverter
from huggingface_hub import hf_hub_download

REPO = os.environ.get("BREEZE_REPO", "MediaTek-Research/Breeze-ASR-25")
REVISION = os.environ.get("BREEZE_REVISION") or None  # 想釘版本就給 commit sha
OUT = os.environ.get("BREEZE_OUT", "/models/breeze-asr-25")
TMP = OUT + ".tmp"
# Breeze 是 large-v2 微調，tokenizer / 前處理與 large-v2 相同；官方 repo 缺檔時用它補
FALLBACK_REPO = "openai/whisper-large-v2"
EXTRA_FILES = ("tokenizer.json", "preprocessor_config.json")

if os.path.exists(os.path.join(OUT, "model.bin")) and "--force" not in sys.argv:
    print(f"{OUT} 已存在，略過（加 --force 重轉）")
    sys.exit(0)

shutil.rmtree(TMP, ignore_errors=True)
print(f"轉換 {REPO}@{REVISION or 'main'} -> {OUT}")
TransformersConverter(REPO, revision=REVISION, low_cpu_mem_usage=True).convert(
    TMP, quantization="float16", force=True)

for name in EXTRA_FILES:
    for repo, rev in ((REPO, REVISION), (FALLBACK_REPO, None)):
        try:
            src = hf_hub_download(repo, name, revision=rev)
        except Exception as exc:  # noqa: BLE001
            print(f"  {repo} 沒有 {name}（{type(exc).__name__}）")
            continue
        shutil.copy(src, os.path.join(TMP, name))
        print(f"  {name} <- {repo}")
        break
    else:
        sys.exit(f"找不到 {name}")

shutil.rmtree(OUT, ignore_errors=True)
os.replace(TMP, OUT)
print("完成：", sorted(os.listdir(OUT)))

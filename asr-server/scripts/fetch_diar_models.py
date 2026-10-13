"""建置映像時下載說話者分辨模型到 /opt/diar-models（含 sha256 驗證）。"""
import os
os.makedirs("/opt/diar-models", exist_ok=True)
import hashlib, io, tarfile, urllib.request
base = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
def get(url, sha):
    data = urllib.request.urlopen(url, timeout=300).read()
    got = hashlib.sha256(data).hexdigest()
    assert got == sha, f"{url}: sha256 {got} != {sha}"
    return data
seg = get(f"{base}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
          "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488")
with tarfile.open(fileobj=io.BytesIO(seg), mode="r:bz2") as t:
    open("/opt/diar-models/segmentation.onnx", "wb").write(
        t.extractfile("sherpa-onnx-pyannote-segmentation-3-0/model.onnx").read())
open("/opt/diar-models/campplus.onnx", "wb").write(get(
    f"{base}/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
    "aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2"))
print("diarization models ok")

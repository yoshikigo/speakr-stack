"""說話者分辨（sherpa-onnx：pyannote segmentation-3.0 + 3D-Speaker CAM++ 聲紋），在 CPU 上跑。

和 GPU 上的 Breeze 轉錄平行執行，所以多數情況下不會增加總時間。
模型在建置映像時下載到 /opt/diar-models（約 35 MB），不需要 PyTorch、也不需要 Hugging Face 帳號。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

log = logging.getLogger("asr.diarize")

MODEL_DIR = os.getenv("ASR_DIAR_MODEL_DIR", "/opt/diar-models")
SEG_MODEL = os.getenv("ASR_DIAR_SEG_MODEL", f"{MODEL_DIR}/segmentation.onnx")
EMB_MODEL = os.getenv("ASR_DIAR_EMB_MODEL", f"{MODEL_DIR}/campplus.onnx")
THREADS = int(os.getenv("ASR_DIAR_THREADS", str(max(2, (os.cpu_count() or 4) // 2))))
# 分群門檻（餘弦距離）：越大 → 越容易把聲音併成同一人；越小 → 越容易拆成更多人
THRESHOLD = float(os.getenv("ASR_DIAR_THRESHOLD", "0.5"))
MIN_ON = float(os.getenv("ASR_DIAR_MIN_DURATION_ON", "0.3"))
MIN_OFF = float(os.getenv("ASR_DIAR_MIN_DURATION_OFF", "0.5"))


@dataclass
class Turn:
    start: float
    end: float
    speaker: int


_sd = None
_lock = threading.Lock()


def available() -> bool:
    return os.path.exists(SEG_MODEL) and os.path.exists(EMB_MODEL)


def _engine(num_clusters: int):
    import sherpa_onnx as so

    global _sd
    cfg = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=SEG_MODEL), num_threads=THREADS),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=EMB_MODEL, num_threads=THREADS),
        clustering=so.FastClusteringConfig(num_clusters=num_clusters, threshold=THRESHOLD),
        min_duration_on=MIN_ON, min_duration_off=MIN_OFF,
    )
    if not cfg.validate():
        raise RuntimeError("sherpa-onnx diarization config 無效（模型檔不存在？）")
    if _sd is None:
        _sd = so.OfflineSpeakerDiarization(cfg)
        log.info("分辨模型已載入（%s threads，threshold %.2f）", THREADS, THRESHOLD)
    else:
        _sd.set_config(cfg)
    return _sd


def _run(samples: np.ndarray, num_clusters: int) -> list[Turn]:
    sd = _engine(num_clusters)
    result = sd.process(samples).sort_by_start_time()
    return [Turn(float(r.start), float(r.end), int(r.speaker)) for r in result]


def diarize(samples: np.ndarray, num_speakers: Optional[int] = None,
            min_speakers: Optional[int] = None, max_speakers: Optional[int] = None) -> list[Turn]:
    """samples：16 kHz mono float32。回傳依時間排序的說話區段。"""
    if min_speakers and max_speakers and min_speakers == max_speakers:
        num_speakers = min_speakers
    t0 = time.monotonic()
    with _lock:
        turns = _run(samples, num_speakers or -1)
        n = len({t.speaker for t in turns})
        # 門檻模式分出的人數超出使用者給的範圍 → 以範圍邊界重新分群
        if not num_speakers:
            if max_speakers and n > max_speakers:
                turns, n = _run(samples, max_speakers), max_speakers
            elif min_speakers and 0 < n < min_speakers:
                turns, n = _run(samples, min_speakers), min_speakers
    log.info("分辨完成：%.0fs 音訊 → %d 位說話者、%d 段，耗時 %.1fs",
             len(samples) / 16000, len({t.speaker for t in turns}), len(turns), time.monotonic() - t0)
    return turns


def assign(words: list[dict], turns: list[Turn], max_gap: float = 2.0) -> list[Optional[int]]:
    """每個字（含 start/end）指派給時間重疊的說話區段；落在區段外的找最近的（max_gap 秒內），否則沿用前一個。"""
    out: list[Optional[int]] = []
    j, prev = 0, None
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        while j < len(turns) and turns[j].end < mid - max_gap:
            j += 1
        best, best_d = None, max_gap
        for t in turns[j:]:
            if t.start > mid + max_gap:
                break
            d = 0.0 if t.start <= mid <= t.end else min(abs(mid - t.start), abs(mid - t.end))
            if d < best_d or (d == 0.0 and best_d == 0.0 and best is None):
                best, best_d = t.speaker, d
                if d == 0.0:
                    break
        out.append(best if best is not None else prev)
        prev = out[-1]
    return out

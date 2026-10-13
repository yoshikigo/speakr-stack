"""OpenAI 相容的語音轉文字服務（faster-whisper / CTranslate2）。

POST /v1/audio/transcriptions  —  與 OpenAI SDK 同樣的 multipart 介面
POST /asr                      —  Speakr asr_endpoint 介面：轉錄 + 說話者分辨（A），
                                  model=whisperx 時轉送給 WhisperX（B），用來 A/B 比較
GET  /v1/models                —  列出可用模型（不會觸發載入）
GET  /health                   —  存活檢查
GET  /metrics                  —  Prometheus 指標
POST /admin/unload             —  立即釋放 VRAM
POST /admin/load?model=...     —  預熱指定模型

設計重點：
* 3090 和 Ollama 共用，所以模型「用到才載入、閒置就卸載」，同時最多只常駐 ASR_MAX_LOADED 個。
* GPU 推論一次只跑一個請求（語音訊息量很小，序列化最單純，也不會互搶 VRAM）。
* 預設不把轉錄內容寫進 log（ASR_LOG_TEXT=1 才會）。
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Optional

import diarize
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

log = logging.getLogger("asr")
logging.basicConfig(level=os.getenv("ASR_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# ---------------------------------------------------------------- 設定

def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _parse_map(spec: str) -> "OrderedDict[str, str]":
    """'a=/x,b=/y' -> {'a': '/x', 'b': '/y'}"""
    out: "OrderedDict[str, str]" = OrderedDict()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "=" not in part:
            raise ValueError(f"格式錯誤（要 name=value）：{part!r}")
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _parse_set(spec: str) -> set[str]:
    return {p.strip() for p in spec.split(",") if p.strip()}


MODELS = _parse_map(_env("ASR_MODELS", "breeze-asr-25=/models/breeze-asr-25,whisper-large-v3=/models/whisper-large-v3"))
DEFAULT_MODEL = _env("ASR_DEFAULT_MODEL", next(iter(MODELS)))
# OpenAI 用戶端（Open WebUI 等）常寫死 whisper-1，對應到預設模型
ALIASES = _parse_map(_env("ASR_ALIASES", f"whisper-1={DEFAULT_MODEL},default={DEFAULT_MODEL}"))

DEVICE = _env("ASR_DEVICE", "cuda")
DEVICE_INDEX = int(_env("ASR_DEVICE_INDEX", "0"))
COMPUTE_TYPE = _env("ASR_COMPUTE_TYPE", "int8_float16")  # float16 品質略好但多吃約一倍 VRAM
MAX_LOADED = max(1, int(_env("ASR_MAX_LOADED", "1")))
IDLE_UNLOAD = int(_env("ASR_IDLE_UNLOAD", "600"))  # 秒；0 = 永不卸載
PRELOAD = _env("ASR_PRELOAD", "")

DEFAULT_LANGUAGE = _env("ASR_LANGUAGE", "zh")  # 空字串 = 自動偵測
# 各模型沒指定語言時用什麼：Breeze 固定中文；large-v3 自動偵測（英文、日文錄音才不會被硬當中文）
MODEL_LANGUAGES = _parse_map(_env("ASR_MODEL_LANGUAGES",
                                  "breeze-asr-25=zh,breeze-asr-26=zh,whisper-large-v3=auto"))


def _lang_for(name: str, language: Optional[str]) -> Optional[str]:
    """呼叫端有給語言就照給的；沒給（或空字串）就用模型預設；auto = 自動偵測。"""
    lang = (language or "").strip() or MODEL_LANGUAGES.get(name, DEFAULT_LANGUAGE)
    lang = (lang or "").strip()
    if not lang or lang.lower() == "auto":
        return None
    return lang.split("-", 1)[0].lower() if "-" in lang else lang
BEAM_SIZE = int(_env("ASR_BEAM_SIZE", "5"))
VAD = _env("ASR_VAD", "1") == "1"
MAX_UPLOAD_MB = int(_env("ASR_MAX_UPLOAD_MB", "50"))

INITIAL_PROMPT = _env("ASR_INITIAL_PROMPT", "以下是台灣華語的語音內容，請使用繁體中文。")
PROMPT_MODELS = _parse_set(_env("ASR_PROMPT_MODELS", "whisper-large-v3"))
OPENCC_CONFIG = _env("ASR_OPENCC", "s2twp")  # 空字串 = 不轉
OPENCC_MODELS = _parse_set(_env("ASR_OPENCC_MODELS", "*"))

# /asr：說話者分辨與 A/B 路由
DIARIZE_DEFAULT = _env("ASR_DIARIZE", "1") == "1"
SEGMENT_MAX_SECONDS = float(_env("ASR_SEGMENT_MAX_SECONDS", "30"))
WHISPERX_URL = _env("ASR_WHISPERX_URL", "").rstrip("/")          # 例：http://127.0.0.1:9010
WHISPERX_MODELS = _parse_set(_env("ASR_WHISPERX_MODELS", "whisperx"))

API_KEY = _env("ASR_API_KEY", "")
LOG_TEXT = _env("ASR_LOG_TEXT", "0") == "1"

if DEFAULT_MODEL not in MODELS:
    raise SystemExit(f"ASR_DEFAULT_MODEL={DEFAULT_MODEL!r} 不在 ASR_MODELS 裡：{list(MODELS)}")


# ---------------------------------------------------------------- 指標

M_REQ = Counter("asr_requests_total", "Transcription requests", ["model", "status"])
M_AUDIO = Counter("asr_audio_seconds_total", "Seconds of audio transcribed", ["model"])
M_PROC = Histogram("asr_processing_seconds", "Wall time per request (incl. load)", ["model"],
                   buckets=(0.5, 1, 2, 3, 5, 8, 13, 21, 34, 60, 120))
M_LOAD = Histogram("asr_model_load_seconds", "Model load time", ["model"], buckets=(1, 2, 4, 8, 16, 32, 64))
M_DIAR = Histogram("asr_diarize_seconds", "Speaker diarization wall time", buckets=(1, 2, 5, 10, 20, 40, 80, 160, 320))
M_ASR = Counter("asr_endpoint_requests_total", "/asr requests by backend", ["backend", "status"])
M_LOADED = Gauge("asr_model_loaded", "1 if the model is resident in memory", ["model"])
for _m in MODELS:
    M_LOADED.labels(_m).set(0)


# ---------------------------------------------------------------- OpenCC

_opencc = None
if OPENCC_CONFIG:
    try:
        import opencc  # type: ignore

        _opencc = opencc.OpenCC(OPENCC_CONFIG)
    except Exception as exc:  # 沒裝或設定錯就照原文輸出，不讓服務起不來
        log.warning("OpenCC (%s) 無法載入，略過繁簡轉換：%s", OPENCC_CONFIG, exc)


def _postprocess(model: str, text: str) -> str:
    if _opencc and ("*" in OPENCC_MODELS or model in OPENCC_MODELS):
        return _opencc.convert(text)
    return text


# ---------------------------------------------------------------- 模型管理

class ModelManager:
    """LRU + 閒置卸載。所有載入/推論都在 self.lock 底下進行。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._loaded: "OrderedDict[str, object]" = OrderedDict()
        self.last_used = 0.0

    def loaded(self) -> list[str]:
        return list(self._loaded)

    def _evict_one(self) -> None:
        name, model = self._loaded.popitem(last=False)
        del model
        gc.collect()
        M_LOADED.labels(name).set(0)
        log.info("已卸載模型 %s", name)

    def get(self, name: str):  # 呼叫端必須持有 self.lock
        if name in self._loaded:
            self._loaded.move_to_end(name)
            return self._loaded[name]
        while len(self._loaded) >= MAX_LOADED:
            self._evict_one()
        from faster_whisper import WhisperModel  # 延後 import，/health 不需要 CUDA

        t0 = time.monotonic()
        model = WhisperModel(MODELS[name], device=DEVICE, device_index=DEVICE_INDEX,
                             compute_type=COMPUTE_TYPE, local_files_only=True)
        dt = time.monotonic() - t0
        M_LOAD.labels(name).observe(dt)
        M_LOADED.labels(name).set(1)
        self._loaded[name] = model
        log.info("已載入模型 %s（%s，%s，%.1fs）", name, DEVICE, COMPUTE_TYPE, dt)
        return model

    def unload_all(self) -> list[str]:  # 呼叫端必須持有 self.lock
        names = self.loaded()
        while self._loaded:
            self._evict_one()
        return names

    def idle_watcher(self) -> None:
        while True:
            time.sleep(30)
            if not self._loaded or IDLE_UNLOAD <= 0:
                continue
            if time.monotonic() - self.last_used < IDLE_UNLOAD:
                continue
            if self.lock.acquire(blocking=False):
                try:
                    if self._loaded and time.monotonic() - self.last_used >= IDLE_UNLOAD:
                        log.info("閒置超過 %ss，釋放 VRAM", IDLE_UNLOAD)
                        self.unload_all()
                finally:
                    self.lock.release()


manager = ModelManager()


def resolve_model(requested: Optional[str]) -> str:
    name = (requested or "").strip() or DEFAULT_MODEL
    name = ALIASES.get(name, name)
    if name not in MODELS:
        raise HTTPException(400, detail={"error": {"message": f"unknown model {name!r}; available: {list(MODELS)}",
                                                   "type": "invalid_request_error"}})
    return name


def _transcribe_blocking(name: str, path: str, language: Optional[str], prompt: Optional[str],
                         temperature: Optional[float]) -> dict:
    with manager.lock:
        model = manager.get(name)
        kwargs: dict = dict(beam_size=BEAM_SIZE, vad_filter=VAD, condition_on_previous_text=False)
        if language:
            kwargs["language"] = language
        if prompt:
            kwargs["initial_prompt"] = prompt
        if temperature and temperature > 0:
            kwargs["temperature"] = temperature
        segments, info = model.transcribe(path, **kwargs)
        segs = list(segments)  # generator：必須在鎖裡跑完
        manager.last_used = time.monotonic()
    text = _postprocess(name, "".join(s.text for s in segs).strip())
    return {
        "text": text,
        "language": info.language,
        "duration": round(float(info.duration), 3),
        "segments": [
            {"id": i, "start": round(s.start, 3), "end": round(s.end, 3),
             "text": _postprocess(name, s.text.strip())}
            for i, s in enumerate(segs)
        ],
    }


def _transcribe_words(name: str, audio, language: Optional[str], prompt: Optional[str]):
    """給 /asr 用：開字級時間戳，回傳 (info, segments)。audio 是 16 kHz float32 numpy。"""
    with manager.lock:
        model = manager.get(name)
        kwargs: dict = dict(beam_size=BEAM_SIZE, vad_filter=VAD, condition_on_previous_text=False,
                            word_timestamps=True)
        if language:
            kwargs["language"] = language
        if prompt:
            kwargs["initial_prompt"] = prompt
        segments, info = model.transcribe(audio, **kwargs)
        segs = list(segments)
        manager.last_used = time.monotonic()
    return info, segs


def _build_segments(name: str, segs, turns) -> list[dict]:
    """依字級時間把每個字分給說話者，說話者一換就切段；同一人講太久也在句界切開。"""
    words = []
    for k, s in enumerate(segs):
        if s.words:
            words += [{"start": w.start, "end": w.end, "word": w.word, "seg": k} for w in s.words]
        elif s.text.strip():
            words.append({"start": s.start, "end": s.end, "word": s.text, "seg": k})
    if not words:
        return []
    speakers = diarize.assign(words, turns) if turns else [0] * len(words)
    labels: dict = {}

    def label(spk):
        if spk is None:
            spk = -1
        if spk not in labels:
            labels[spk] = f"SPEAKER_{len(labels):02d}"
        return labels[spk]

    out, cur = [], None
    for w, spk in zip(words, speakers):
        lab = label(spk if spk is not None else (cur["_raw"] if cur else None))
        new = (cur is None or lab != cur["speaker"]
               or (w["seg"] != cur["_seg"] and w["start"] - cur["start"] >= SEGMENT_MAX_SECONDS))
        if new:
            if cur:
                out.append(cur)
            cur = {"start": w["start"], "end": w["end"], "text": "", "speaker": lab, "_seg": w["seg"], "_raw": spk}
        cur["text"] += w["word"]
        cur["end"] = w["end"]
        cur["_seg"] = w["seg"]
    out.append(cur)
    result = []
    for seg in out:
        text = _postprocess(name, seg["text"].strip())
        if text:
            result.append({"start": round(seg["start"], 3), "end": round(seg["end"], 3),
                           "text": text, "speaker": seg["speaker"]})
    return result


# ---------------------------------------------------------------- 字幕格式

def _ts(sec: float, sep: str) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _srt(segs: list[dict]) -> str:
    return "\n".join(f"{i + 1}\n{_ts(s['start'], ',')} --> {_ts(s['end'], ',')}\n{s['text']}\n"
                     for i, s in enumerate(segs))


def _vtt(segs: list[dict]) -> str:
    body = "\n".join(f"{_ts(s['start'], '.')} --> {_ts(s['end'], '.')}\n{s['text']}\n" for s in segs)
    return "WEBVTT\n\n" + body


# ---------------------------------------------------------------- HTTP

@asynccontextmanager
async def lifespan(_app: FastAPI):
    await _startup()
    yield


app = FastAPI(title="asr-server", version="1.0", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def _openai_style_errors(_request: Request, exc: HTTPException) -> JSONResponse:
    # OpenAI SDK 期待 {"error": {...}}，不是 FastAPI 預設的 {"detail": ...}
    body = exc.detail if isinstance(exc.detail, dict) else {"error": {"message": str(exc.detail)}}
    return JSONResponse(body, status_code=exc.status_code)


def require_key(request: Request) -> None:
    if not API_KEY:
        return
    if request.headers.get("authorization", "") != f"Bearer {API_KEY}":
        raise HTTPException(401, detail={"error": {"message": "invalid api key", "type": "authentication_error"}})


async def _startup() -> None:
    threading.Thread(target=manager.idle_watcher, name="idle-unload", daemon=True).start()
    log.info("models=%s default=%s aliases=%s device=%s compute=%s max_loaded=%d idle_unload=%ss lang=%r",
             dict(MODELS), DEFAULT_MODEL, dict(ALIASES), DEVICE, COMPUTE_TYPE, MAX_LOADED, IDLE_UNLOAD,
             DEFAULT_LANGUAGE)
    if PRELOAD:
        name = resolve_model(PRELOAD)

        def _warm() -> None:
            with manager.lock:
                manager.get(name)
                manager.last_used = time.monotonic()

        await asyncio.to_thread(_warm)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "models": list(MODELS), "default": DEFAULT_MODEL, "loaded": manager.loaded(),
            "diarization": diarize.available(), "whisperx": WHISPERX_URL or None}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/models", dependencies=[Depends(require_key)])
async def list_models() -> dict:
    return {"object": "list",
            "data": [{"id": m, "object": "model", "owned_by": "local"}
                     for m in list(MODELS) + list(ALIASES) + (sorted(WHISPERX_MODELS) if WHISPERX_URL else [])]}


_CUDA_MARKERS = ("CUDA", "cuda", "CUBLAS", "cudnn", "invalid device ordinal")


def _cuda_broken(exc: Exception) -> bool:
    """CUDA 出錯（OOM 之後常常整個 context 壞掉，之後每個請求都 invalid device ordinal）。
    唯一可靠的恢復方式是重開行程：回 503 後 1 秒結束，交給 systemd（Restart=on-failure）重啟，
    VRAM 也會一起釋放。"""
    if not any(m in str(exc) for m in _CUDA_MARKERS):
        return False
    log.critical("CUDA 錯誤（%s），1 秒後重啟 asr-server 以恢復 GPU 狀態", exc)
    threading.Timer(1.0, lambda: os._exit(3)).start()
    return True


def _cuda_error_response() -> JSONResponse:
    return JSONResponse({"error": {"message": "GPU 記憶體不足或 CUDA 狀態異常，asr-server 正在重啟，請幾分鐘後重試"
                                              "（先確認 nvidia-smi 有空出 VRAM）", "type": "server_error"}},
                        status_code=503)


@app.post("/v1/audio/transcriptions", dependencies=[Depends(require_key)])
async def transcriptions(
    file: UploadFile = File(...),
    model: Optional[str] = Form(None),
    language: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    response_format: str = Form("json"),
    temperature: Optional[float] = Form(None),
):
    name = resolve_model(model)
    fmt = (response_format or "json").lower()
    if fmt not in {"json", "text", "verbose_json", "srt", "vtt"}:
        raise HTTPException(400, detail={"error": {"message": f"unsupported response_format {fmt!r}",
                                                   "type": "invalid_request_error"}})
    lang = _lang_for(name, language)
    use_prompt = prompt or (INITIAL_PROMPT if name in PROMPT_MODELS else None)

    suffix = os.path.splitext(file.filename or "")[1] or ".audio"
    limit = MAX_UPLOAD_MB * 1024 * 1024
    t0 = time.monotonic()
    tmp = tempfile.NamedTemporaryFile(prefix="asr-", suffix=suffix, delete=False)
    try:
        size = 0
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, detail={"error": {"message": f"file exceeds {MAX_UPLOAD_MB} MB",
                                                           "type": "invalid_request_error"}})
            tmp.write(chunk)
        tmp.close()
        try:
            result = await asyncio.to_thread(_transcribe_blocking, name, tmp.name, lang, use_prompt, temperature)
        except HTTPException:
            raise
        except Exception as exc:
            M_REQ.labels(name, "error").inc()
            log.exception("轉錄失敗（%s，%d bytes）", name, size)
            if _cuda_broken(exc):
                return _cuda_error_response()
            msg = str(exc)
            status = 400 if "Invalid data" in msg or "decode" in msg.lower() else 500
            return JSONResponse({"error": {"message": msg, "type": "server_error"}}, status_code=status)
    finally:
        try:
            tmp.close()
            os.unlink(tmp.name)
        except OSError:
            pass

    dt = time.monotonic() - t0
    M_REQ.labels(name, "ok").inc()
    M_AUDIO.labels(name).inc(result["duration"])
    M_PROC.labels(name).observe(dt)
    log.info("%s：音訊 %.1fs，耗時 %.2fs，語言 %s，%d 字%s", name, result["duration"], dt, result["language"],
             len(result["text"]), f"：{result['text']}" if LOG_TEXT else "")

    if fmt == "text":
        return PlainTextResponse(result["text"])
    if fmt == "srt":
        return PlainTextResponse(_srt(result["segments"]))
    if fmt == "vtt":
        return PlainTextResponse(_vtt(result["segments"]), media_type="text/vtt")
    if fmt == "verbose_json":
        return {"task": "transcribe", **result}
    return {"text": result["text"]}


async def _save_upload(upload: UploadFile) -> tuple[str, int]:
    suffix = os.path.splitext(upload.filename or "")[1] or ".audio"
    limit = MAX_UPLOAD_MB * 1024 * 1024
    tmp = tempfile.NamedTemporaryFile(prefix="asr-", suffix=suffix, delete=False)
    size = 0
    try:
        while chunk := await upload.read(1 << 20):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, detail={"error": {"message": f"file exceeds {MAX_UPLOAD_MB} MB",
                                                           "type": "invalid_request_error"}})
            tmp.write(chunk)
    except BaseException:
        tmp.close()
        os.unlink(tmp.name)
        raise
    tmp.close()
    return tmp.name, size


async def _forward_whisperx(path: str, filename: str, params: dict) -> Response:
    import httpx

    if not WHISPERX_URL:
        raise HTTPException(503, detail={"error": {"message": "WhisperX 沒有設定（ASR_WHISPERX_URL）"}})
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10)) as client:
            with open(path, "rb") as fh:
                r = await client.post(f"{WHISPERX_URL}/asr", params=params,
                                      files={"audio_file": (filename, fh, "application/octet-stream")})
    except httpx.HTTPError as exc:
        M_ASR.labels("whisperx", "error").inc()
        raise HTTPException(502, detail={"error": {"message": f"連不到 WhisperX：{exc}"}})
    M_ASR.labels("whisperx", "ok" if r.status_code == 200 else "error").inc()
    log.info("/asr → WhisperX：HTTP %s，耗時 %.1fs", r.status_code, time.monotonic() - t0)
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))


@app.post("/asr", dependencies=[Depends(require_key)])
async def asr(
    audio_file: UploadFile = File(...),
    model: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    initial_prompt: Optional[str] = Query(None),
    hotwords: Optional[str] = Query(None),
    diarize_q: Optional[bool] = Query(None, alias="diarize"),
    enable_diarization: Optional[bool] = Query(None),
    num_speakers: Optional[int] = Query(None),
    min_speakers: Optional[int] = Query(None),
    max_speakers: Optional[int] = Query(None),
    return_speaker_embeddings: Optional[bool] = Query(None),
):
    """Speakr asr_endpoint 相容。model=whisperx → 轉送 WhisperX（B）；其他 → 本機轉錄 + sherpa-onnx 分辨（A）。"""
    path, size = await _save_upload(audio_file)
    try:
        if (model or "").strip() in WHISPERX_MODELS:
            params = {k: v for k, v in {
                "output": "json", "task": "transcribe", "language": language, "initial_prompt": initial_prompt,
                "hotwords": hotwords, "diarize": diarize_q, "enable_diarization": enable_diarization,
                "num_speakers": num_speakers, "min_speakers": min_speakers, "max_speakers": max_speakers,
                "return_speaker_embeddings": return_speaker_embeddings}.items() if v is not None}
            return await _forward_whisperx(path, audio_file.filename or "audio", params)

        name = resolve_model(model)
        lang = _lang_for(name, language)
        prompt = " ".join(p for p in (initial_prompt, hotwords) if p) or \
            (INITIAL_PROMPT if name in PROMPT_MODELS else None)
        want = DIARIZE_DEFAULT if diarize_q is None and enable_diarization is None else bool(diarize_q or enable_diarization)
        do_diar = want and diarize.available()

        from faster_whisper.audio import decode_audio

        t0 = time.monotonic()
        audio = await asyncio.to_thread(decode_audio, path, 16000)
        duration = len(audio) / 16000

        async def _diar():
            if not do_diar:
                return []
            t = time.monotonic()
            turns = await asyncio.to_thread(diarize.diarize, audio, num_speakers, min_speakers, max_speakers)
            M_DIAR.observe(time.monotonic() - t)
            return turns

        # GPU 轉錄和 CPU 分辨同時跑
        (info, segs), turns = await asyncio.gather(
            asyncio.to_thread(_transcribe_words, name, audio, lang, prompt), _diar())
        segments = _build_segments(name, segs, turns)
    except HTTPException:
        raise
    except Exception as exc:
        M_ASR.labels("local", "error").inc()
        log.exception("/asr 失敗（%s，%d bytes）", model, size)
        if _cuda_broken(exc):
            return _cuda_error_response()
        return JSONResponse({"error": {"message": str(exc), "type": "server_error"}}, status_code=500)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    dt = time.monotonic() - t0
    n_spk = len({s["speaker"] for s in segments})
    M_ASR.labels("local", "ok").inc()
    M_REQ.labels(name, "ok").inc()
    M_AUDIO.labels(name).inc(duration)
    M_PROC.labels(name).observe(dt)
    log.info("/asr %s：音訊 %.0fs，耗時 %.1fs，%d 段，%d 位說話者%s", name, duration, dt, len(segments), n_spk,
             "" if do_diar else "（未分辨）")
    # 不回傳 text：Speakr 會用 segments 自己組出「[SPEAKER_00]: …」的全文
    return {"language": info.language, "duration": round(duration, 3), "segments": segments,
            "speakers": n_spk, "backend": "asr-server+sherpa-onnx", "model": name}


@app.post("/admin/unload", dependencies=[Depends(require_key)])
async def admin_unload() -> dict:
    def _do() -> list[str]:
        with manager.lock:
            return manager.unload_all()

    return {"unloaded": await asyncio.to_thread(_do)}


@app.post("/admin/load", dependencies=[Depends(require_key)])
async def admin_load(model: Optional[str] = Query(None)) -> dict:
    name = resolve_model(model)

    def _do() -> None:
        with manager.lock:
            manager.get(name)
            manager.last_used = time.monotonic()

    await asyncio.to_thread(_do)
    return {"loaded": manager.loaded()}

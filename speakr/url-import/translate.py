"""繁體中文翻譯：把摘要與逐字稿翻成台灣繁體中文，存在 speakr-url 自己的 volume，原文完全不動。

  GET  /api/translation/<rid>          → 已存的翻譯 + 進行中的工作狀態
  POST /api/translation {"recording_id"} → 開始翻譯（先摘要、再逐字稿）

權限：一律用使用者自己的 Speakr session 讀錄音，讀得到才給看、才能翻。
Ollama 同一時間只跑一個翻譯工作（不跟摘要、人名查證搶太兇）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("translate")

SPEAKR = os.getenv("SPEAKR_URL", "http://127.0.0.1:8899").rstrip("/")
OLLAMA = os.getenv("OLLAMA_URL", "http://127.0.0.1:11435").rstrip("/")
MODEL = os.getenv("TRANSLATE_MODEL", os.getenv("NAMECHECK_MODEL", "qwen3.8-27b-ud-64k"))
STORE = Path(os.getenv("TRANSLATE_DIR", "/data/translations"))
BATCH_LINES = int(os.getenv("TRANSLATE_BATCH_LINES", "30"))
BATCH_CHARS = int(os.getenv("TRANSLATE_BATCH_CHARS", "2000"))
TZ = ZoneInfo(os.getenv("TZ", "Asia/Taipei"))

_jobs: dict[int, dict] = {}          # rid -> {state, step, error, user_id, updated}
_jobs_lock = threading.Lock()
_ollama_slot = threading.Semaphore(1)
_access: dict[tuple[str, int], tuple[float, dict]] = {}
_access_lock = threading.Lock()


# ---------------------------------------------------------------- 工具

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _path(rid: int) -> Path:
    return STORE / f"{int(rid)}.json"


def load(rid: int) -> dict:
    try:
        return json.loads(_path(rid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(rid: int, data: dict) -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    tmp = _path(rid).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _path(rid))


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


def fetch_recording(rid: int, auth: dict[str, str], use_cache: bool = True) -> dict:
    """用使用者的 session 讀錄音（同時就是權限檢查）。輪詢時 60 秒內用快取。"""
    key = (hashlib.sha256(auth.get("cookie", "").encode()).hexdigest(), rid)
    now = time.time()
    if use_cache:
        with _access_lock:
            hit = _access.get(key)
            if hit and now - hit[0] < 60:
                return hit[1]
    r = httpx.get(f"{SPEAKR}/api/v1/recordings/{rid}", params={"include": "summary"}, headers=auth,
                  timeout=30, follow_redirects=False)
    r.raise_for_status()
    rec = r.json()
    with _access_lock:
        if len(_access) > 512:
            _access.clear()
        _access[key] = (now, rec)
    return rec


def _ollama(system: str, user: str, schema: dict | None = None, num_predict: int = 8192) -> str:
    body = {"model": MODEL, "stream": False, "think": False,
            "options": {"temperature": 0.2, "num_predict": num_predict},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if schema:
        body["format"] = schema
    r = httpx.post(f"{OLLAMA}/api/chat", json=body, timeout=1800)
    r.raise_for_status()
    return r.json()["message"]["content"]


SUMMARY_PROMPT = (
    "你是專業譯者。把使用者提供的 Markdown 文件完整翻譯成台灣繁體中文，使用台灣慣用語。"
    "保留原本的 Markdown 結構（標題、清單、粗體、表格、連結），網址、程式碼、數字不要改。"
    "中文人名照原文；外國人名第一次出現寫成「中文譯名（原文）」，之後用中文譯名。"
    "原文已經是中文的部分，轉成繁體並改成台灣用語即可，不要改寫意思。"
    "只輸出譯文本身，不要加前言、說明或程式碼區塊標記。"
)

LINES_PROMPT = (
    "你是專業逐字稿譯者。lines 是一段對話逐字稿，依序逐行翻譯成台灣繁體中文（台灣用語、口語自然）。"
    "translations 的數量必須和 lines 完全相同、順序一一對應；不要合併、拆開或省略任何一行。"
    "原文已經是中文的行，轉成繁體並改成台灣用語即可。人名照原文，外國人名可以寫「譯名（原文）」。"
    "context 只是前文參考，不要翻譯它。"
)

LINES_SCHEMA = {"type": "object",
                "properties": {"translations": {"type": "array", "items": {"type": "string"}}},
                "required": ["translations"]}


def translate_summary(text: str) -> str:
    out = _ollama(SUMMARY_PROMPT, text).strip()
    out = re.sub(r"^```(?:markdown|md)?\s*\n", "", out)
    return re.sub(r"\n```\s*$", "", out).strip()


def translate_lines(lines: list[str], context: list[str]) -> list[str]:
    """一批逐字稿。模型回的行數不對就對半拆開重試，最小到一行。"""
    if not lines:
        return []
    payload = json.dumps({"context": context[-3:], "lines": lines}, ensure_ascii=False)
    try:
        got = json.loads(_ollama(LINES_PROMPT, payload, LINES_SCHEMA,
                                 num_predict=max(1024, sum(len(x) for x in lines) * 4))).get("translations", [])
    except (ValueError, KeyError):
        got = []
    if len(got) == len(lines) and all(isinstance(x, str) for x in got):
        return [x.strip() for x in got]
    if len(lines) == 1:
        return [got[0].strip() if got and isinstance(got[0], str) else lines[0]]
    mid = len(lines) // 2
    log.info("行數對不上（%d → %d），拆成兩半重試", len(lines), len(got))
    return translate_lines(lines[:mid], context) + translate_lines(lines[mid:], context + lines[:mid])


def _batches(lines: list[str]) -> list[tuple[int, int]]:
    out, start, chars = [], 0, 0
    for i, line in enumerate(lines):
        if i > start and (i - start >= BATCH_LINES or chars + len(line) > BATCH_CHARS):
            out.append((start, i))
            start, chars = i, 0
        chars += len(line)
    if start < len(lines):
        out.append((start, len(lines)))
    return out


def fetch_transcript(rid: int, auth: dict[str, str]) -> list[dict]:
    """[{start, speaker, text}]；純文字逐字稿就逐段切開（沒有時間與說話者）。"""
    r = httpx.get(f"{SPEAKR}/api/v1/recordings/{rid}/transcript", params={"format": "json"},
                  headers=auth, timeout=60, follow_redirects=False)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    data = r.json()
    segs = []
    for s in data.get("segments") or []:
        text = (s.get("text") or s.get("sentence") or "").strip()
        if text:
            segs.append({"start": s.get("start_time"), "speaker": s.get("speaker") or "", "text": text})
    if not segs and data.get("raw"):
        segs = [{"start": None, "speaker": "", "text": p.strip()}
                for p in re.split(r"\n\s*\n|\n", data["raw"]) if p.strip()]
    return segs


# ---------------------------------------------------------------- 工作

def _set(rid: int, **kw) -> None:
    with _jobs_lock:
        _jobs.setdefault(rid, {}).update(kw, updated=time.time())


def job_of(rid: int) -> dict | None:
    with _jobs_lock:
        j = _jobs.get(rid)
        return {k: v for k, v in j.items() if k != "user_id"} if j else None


def start(rid: int, auth: dict[str, str], user_id) -> tuple[int, dict]:
    with _jobs_lock:
        j = _jobs.get(rid)
        if j and j.get("state") in ("queued", "running"):
            return 409, {"error": "這份錄音正在翻譯中"}
        _jobs[rid] = {"state": "queued", "step": "排隊中（等其他翻譯完成）", "error": "",
                      "user_id": user_id, "updated": time.time()}
    threading.Thread(target=_run, args=(rid, dict(auth)), name=f"translate-{rid}", daemon=True).start()
    return 202, {"job": job_of(rid)}


def _run(rid: int, auth: dict[str, str]) -> None:
    try:
        with _ollama_slot:
            _set(rid, state="running", step="讀取錄音")
            rec = fetch_recording(rid, auth, use_cache=False)
            data = load(rid)
            data["title"] = rec.get("title") or ""

            summary = rec.get("summary") or ""
            if summary and not summary.startswith("[Summary not generated"):
                _set(rid, step="翻譯摘要")
                t0 = time.time()
                data["summary"] = {"text": translate_summary(summary), "source_hash": _sha(summary),
                                   "translated_at": _now(), "model": MODEL}
                _save(rid, data)
                log.info("recording %s 摘要翻譯完成（%.0fs）", rid, time.time() - t0)

            _set(rid, step="讀取逐字稿")
            segs = fetch_transcript(rid, auth)
            if segs:
                texts = [s["text"] for s in segs]
                src_hash = _sha("\n".join(texts))
                old = data.get("transcript") or {}
                if old.get("source_hash") == src_hash and old.get("complete"):
                    log.info("recording %s 逐字稿沒變，沿用舊翻譯", rid)
                else:
                    out: list[str] = []
                    batches = _batches(texts)
                    t0 = time.time()
                    for n, (a, b) in enumerate(batches, 1):
                        _set(rid, step=f"翻譯逐字稿 {n}/{len(batches)}")
                        out += translate_lines(texts[a:b], texts[max(0, a - 3):a])
                        # 每批都存，看得到進度，也不怕中途失敗全部白做
                        data["transcript"] = {
                            "segments": [{**s, "translation": out[i] if i < len(out) else ""}
                                         for i, s in enumerate(segs)],
                            "source_hash": src_hash, "complete": len(out) >= len(segs),
                            "translated_at": _now(), "model": MODEL}
                        _save(rid, data)
                    log.info("recording %s 逐字稿翻譯完成：%d 行 %d 批（%.0fs）",
                             rid, len(segs), len(batches), time.time() - t0)
            _set(rid, state="done", step="")
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        msg = {401: "登入逾時，請重新整理頁面", 403: "沒有這份錄音的權限", 404: "找不到這份錄音"}.get(
            code, f"HTTP {code}（{exc.request.url.host}:{exc.request.url.port}）")
        _set(rid, state="error", error=msg)
    except httpx.HTTPError as exc:
        log.warning("recording %s 翻譯連線失敗：%s", rid, exc)
        _set(rid, state="error", error="連不到 Ollama 或 Speakr（看 journalctl --user -u speakr-url）")
    except Exception:  # noqa: BLE001
        log.exception("recording %s 翻譯失敗", rid)
        _set(rid, state="error", error="翻譯失敗（看 journalctl --user -u speakr-url）")


def view(rid: int, rec: dict) -> dict:
    """GET 回傳：已存翻譯 + 摘要是否已經改過（翻譯過期）+ 工作狀態。"""
    data = load(rid)
    summary = rec.get("summary") or ""
    if data.get("summary"):
        data["summary"]["stale"] = bool(summary) and _sha(summary) != data["summary"].get("source_hash")
    data["job"] = job_of(rid)
    data["status"] = rec.get("status") or ""
    data["recording_id"] = rid
    return data


def gc_jobs() -> None:
    cutoff = time.time() - 24 * 3600
    with _jobs_lock:
        for rid in [r for r, j in _jobs.items() if j.get("updated", 0) < cutoff]:
            _jobs.pop(rid, None)

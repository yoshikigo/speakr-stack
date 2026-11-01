"""一鍵匯入 Libratory：把這筆錄音交給同一台機器上的 Libratory，做成一本「用原始錄音朗讀」的書。

  POST /api/libratory {"recording_id": 12}
    → 202 {"book_id": "...", "url": "https://…/books/…", "existing": false}   Libratory 開始匯入
    → 200 {..., "existing": true}                                               已經匯入過，回同一本
  GET  /api/libratory/<book_id>
    → 200 {"status": "extracting" | "pending" | "failed", "error": ..., "chapters": n, "url": ...}

章節、說話者名字、標點整理、切音檔都在 Libratory 那邊做（它用自己的 Speakr API token 讀逐字稿和音檔）；
這裡只負責：確認按按鈕的人在 Speakr 看得到這筆錄音，再請 Libratory 開始。
"""

from __future__ import annotations

import logging
import os
import re

import httpx

log = logging.getLogger("libratory")

SPEAKR = os.getenv("SPEAKR_URL", "http://127.0.0.1:8899").rstrip("/")
LIBRATORY = os.getenv("LIBRATORY_URL", "http://127.0.0.1:3034").rstrip("/")
# Libratory 回的書本網址用它自己的 PUBLIC_ORIGIN；沒設時用這個組（例如 https://arch.<tailnet>.ts.net:8448）
LIBRATORY_PUBLIC = os.getenv("LIBRATORY_PUBLIC_URL", "").rstrip("/")
BOOK_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

enabled = bool(LIBRATORY)


def _book_url(book_id: str, given: str | None) -> str | None:
    if given:
        return given
    return f"{LIBRATORY_PUBLIC}/books/{book_id}" if LIBRATORY_PUBLIC else None


def start(rid: int, auth: dict[str, str]) -> tuple[int, dict]:
    # 權限：用按按鈕那個人的 Speakr session 讀一次錄音，看不到就不准匯入
    r = httpx.get(f"{SPEAKR}/api/v1/recordings/{rid}", headers=auth, timeout=30, follow_redirects=False)
    r.raise_for_status()
    try:
        res = httpx.post(f"{LIBRATORY}/api/speakr/recordings/{rid}/import", json={}, timeout=120)
    except httpx.HTTPError as exc:
        log.warning("連不到 Libratory：%s", exc)
        return 502, {"error": f"連不到 Libratory（{LIBRATORY}）"}
    try:
        data = res.json()
    except ValueError:
        data = {}
    if res.status_code in (200, 202):
        book_id = str(data.get("bookId", ""))
        return res.status_code, {"book_id": book_id, "existing": bool(data.get("existing")), "url": _book_url(book_id, data.get("url"))}
    message = data.get("message") or data.get("error") or f"HTTP {res.status_code}"
    if data.get("error") == "not_configured":
        message = "Libratory 還沒設定 Speakr：到 Libratory 的 Settings 貼上 Speakr 的 API token（read 權限）"
    elif data.get("error") == "not_ready":
        message = f"逐字稿還不能用：{message}"
    return (res.status_code if res.status_code in (400, 404, 409, 412) else 502), {"error": message}


def status(book_id: str) -> tuple[int, dict]:
    if not BOOK_ID.fullmatch(book_id):
        return 400, {"error": "bad request"}
    try:
        res = httpx.get(f"{LIBRATORY}/api/books/{book_id}", timeout=30)
    except httpx.HTTPError:
        return 502, {"error": "連不到 Libratory"}
    if res.status_code == 404:
        return 404, {"error": "Libratory 找不到這本書（可能被刪掉了）"}
    if res.status_code != 200:
        return 502, {"error": f"Libratory 回 HTTP {res.status_code}"}
    data = res.json()
    return 200, {
        "status": data.get("status"),
        "error": data.get("error"),
        "chapters": len(data.get("chapters") or []),
        "title": data.get("title"),
        "url": _book_url(book_id, None),
    }

"""人名查證：摘要完成後，上網確認摘要裡的人名寫法，修正明顯的同音錯字並附上來源。

觸發方式：
  - Speakr webhook recording.summary.completed（自動，用 API token，只改摘要）
  - 逐字稿工具列的「校正姓名」按鈕（用使用者自己的登入身分；摘要由這裡改，
    逐字稿由瀏覽器拿回修正清單後用 Speakr 自己的編輯 API 存回）
  - 手動 `python namecheck.py <recording_id>`

流程：
  1. Speakr API 取標題、備註（網址匯入時存的影片說明）、摘要
  2. Ollama（不開思考）列出摘要中的真實人物與身分
  3. SearXNG 以「姓名 身分」搜尋每個人
  4. Ollama 對照搜尋結果與影片標題/說明，判斷 正確 / 需修正 / 無法確認
  5. 只有高信心的修正才替換摘要文字；所有結果附在摘要最後的「人名查證」段落，含來源連結

重跑會先移除舊的查證段落。沒有摘要時改從逐字稿找人名（只回傳修正清單，不寫摘要）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("namecheck")

SPEAKR = os.getenv("SPEAKR_URL", "http://127.0.0.1:8899").rstrip("/")
API_TOKEN = os.getenv("SPEAKR_API_TOKEN", "")
WEBHOOK_SECRET = os.getenv("SPEAKR_WEBHOOK_SECRET", "")
SEARXNG = os.getenv("SEARXNG_URL", "http://127.0.0.1:8090").rstrip("/")
OLLAMA = os.getenv("OLLAMA_URL", "http://127.0.0.1:11435").rstrip("/")
MODEL = os.getenv("NAMECHECK_MODEL", "qwen3.8-27b-ud-64k")
MAX_PEOPLE = int(os.getenv("NAMECHECK_MAX_PEOPLE", "12"))
MIN_CONFIDENCE = float(os.getenv("NAMECHECK_MIN_CONFIDENCE", "0.8"))
TZ = ZoneInfo(os.getenv("TZ", "Asia/Taipei"))
SECTION = "## 人名查證（自動）"

enabled = bool(API_TOKEN)
_busy: set[int] = set()
_busy_lock = threading.Lock()


# ---------------------------------------------------------------- 外部服務

def speakr(method: str, path: str, auth: dict[str, str] | None = None, **kw) -> httpx.Response:
    """auth=None 用 API token；按鈕觸發時傳入使用者的 cookie 標頭，權限就跟使用者本人一樣。"""
    headers = auth if auth is not None else {"Authorization": f"Bearer {API_TOKEN}"}
    r = httpx.request(method, f"{SPEAKR}/api/v1{path}", headers=headers, timeout=60, follow_redirects=False, **kw)
    r.raise_for_status()
    return r


def ollama_json(system: str, user: str, schema: dict) -> dict:
    """Ollama 原生 /api/chat：關閉思考、用 JSON schema 約束輸出。"""
    r = httpx.post(f"{OLLAMA}/api/chat", timeout=600, json={
        "model": MODEL, "stream": False, "think": False, "format": schema,
        "options": {"temperature": 0.1, "num_predict": 4096},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    })
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


def search(query: str, n: int = 5) -> list[dict]:
    r = httpx.get(f"{SEARXNG}/search", timeout=30,
                  params={"q": query, "format": "json", "language": "zh-TW", "safesearch": 0})
    r.raise_for_status()
    out = []
    for it in r.json().get("results", [])[:n]:
        out.append({"title": (it.get("title") or "")[:120], "snippet": (it.get("content") or "")[:300],
                    "url": it.get("url", "")})
    return out


# ---------------------------------------------------------------- 主流程

PEOPLE_SCHEMA = {
    "type": "object",
    "properties": {"people": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "role": {"type": "string"}},
        "required": ["name", "role"]}}},
    "required": ["people"],
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "status": {"type": "string", "enum": ["ok", "fix", "unsure"]},
            "correct_name": {"type": "string"},
            "confidence": {"type": "number"},
            "source_url": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["name", "status", "correct_name", "confidence", "source_url", "reason"]}}},
    "required": ["results"],
}


def strip_section(summary: str) -> str:
    i = summary.find(SECTION)
    return summary[:i].rstrip().removesuffix("---").rstrip() if i >= 0 else summary


def replace_safe(text: str, wrong: str, right: str) -> str:
    """換掉錯的寫法，但已經是正確寫法的地方不動（王明 → 王明德 重跑不會變成 王明德德）。"""
    return right.join(piece.replace(wrong, right) for piece in text.split(right))


def check_recording(rid: int, auth: dict[str, str] | None = None, progress=lambda _msg: None) -> dict:
    """回傳 {"message", "people", "fixed", "fixes": [[錯, 對], ...], "summary_updated"}。"""
    progress("讀取錄音")
    rec = speakr("GET", f"/recordings/{rid}", auth, params={"include": "summary,notes"}).json()
    title = rec.get("title") or ""
    notes = (rec.get("notes") or "")[:2000]
    summary = strip_section(rec.get("summary") or "")
    has_summary = bool(summary) and not summary.startswith("[Summary not generated")
    if has_summary:
        source, text = "摘要", summary
    else:
        r = httpx.get(f"{SPEAKR}/api/v1/recordings/{rid}/transcript", params={"format": "text"}, timeout=60,
                      headers=auth if auth is not None else {"Authorization": f"Bearer {API_TOKEN}"})
        text = r.text if r.status_code == 200 else ""
        source = "逐字稿"
    if not text.strip():
        return {"message": "沒有摘要也沒有逐字稿，略過", "people": 0, "fixed": 0, "fixes": [], "summary_updated": False}

    # 1) 文中的人
    progress(f"從{source}找出人名")
    people = ollama_json(
        f"你是新聞事實查核助理。從{source}中列出所有真實人物的姓名，以及他們在文中的身分（職稱、所屬單位、在節目中的角色）。"
        f"只列人名；不要列節目、媒體、政黨或其他組織。姓名照{source}原樣抄寫，不要自行更正。",
        f"錄音標題：{title}\n\n{source}：\n{text[:12000]}",
        PEOPLE_SCHEMA,
    ).get("people", [])
    seen, uniq = set(), []
    for p in people:
        name = (p.get("name") or "").strip()
        if name and name not in seen and len(name) <= 20:
            seen.add(name)
            uniq.append({"name": name, "role": (p.get("role") or "").strip()[:60]})
    people = uniq[:MAX_PEOPLE]
    if not people:
        return {"message": f"{source}中沒有人名", "people": 0, "fixed": 0, "fixes": [], "summary_updated": False}

    # 2) 搜尋
    evidence = []
    for i, p in enumerate(people, 1):
        progress(f"上網查證 {i}/{len(people)}：{p['name']}")
        try:
            hits = search(f"{p['name']} {p['role']}".strip())
        except Exception as exc:  # noqa: BLE001
            log.warning("搜尋失敗 %s：%s", p["name"], exc)
            hits = []
        evidence.append({**p, "search_results": hits})
        time.sleep(1)  # 對 SearXNG 背後的搜尋引擎客氣一點

    # 3) 判斷
    progress("比對搜尋結果")
    verdicts = ollama_json(
        "你是新聞事實查核助理，負責檢查語音辨識造成的人名錯字（常見是同音或近音字，例如「揚／洋」「誠／成」）。"
        "對每個人：如果搜尋結果或影片標題/說明清楚顯示同一個人（身分相符）的正確寫法與摘要不同，status=fix 並填 correct_name；"
        "如果寫法正確，status=ok，correct_name 填原名；證據不足、或可能是不同的人，status=unsure。"
        "confidence 是 0 到 1 的把握程度。source_url 填最能佐證的搜尋結果網址（沒有就空字串）。"
        "reason 用繁體中文一句話說明。不要憑記憶判斷，只依據提供的資料。",
        json.dumps({"video_title": title, "video_description": notes, "people": evidence}, ensure_ascii=False),
        VERDICT_SCHEMA,
    ).get("results", [])
    by_name = {v.get("name"): v for v in verdicts}

    # 4) 套用 + 附查證段落
    lines, fixes = [], []
    for p in people:
        v = by_name.get(p["name"]) or {"status": "unsure", "reason": "模型沒有回覆", "source_url": "", "confidence": 0}
        role = f"（{p['role']}）" if p["role"] else ""
        src = f" [來源]({v['source_url']})" if v.get("source_url", "").startswith("http") else ""
        correct = (v.get("correct_name") or "").strip()
        if (v.get("status") == "fix" and correct and correct != p["name"] and len(p["name"]) >= 2
                and v.get("confidence", 0) >= MIN_CONFIDENCE):
            summary = replace_safe(summary, p["name"], correct)
            fixes.append([p["name"], correct])
            lines.append(f"- ✏️ {p['name']} → **{correct}**{role}：{v.get('reason', '')}{src}")
        elif v.get("status") == "fix" and correct and correct != p["name"]:
            lines.append(f"- ❓ {p['name']}{role}：可能應為「{correct}」，把握度不足未自動修改。{v.get('reason', '')}{src}")
        elif v.get("status") == "ok":
            lines.append(f"- ✅ {p['name']}{role}{src}")
        else:
            lines.append(f"- ❓ {p['name']}{role}：{v.get('reason') or '查無足夠資料，請人工確認'}{src}")

    fixed = len(fixes)
    result = {"people": len(people), "fixed": fixed, "fixes": fixes, "summary_updated": False,
              "message": f"查證 {len(people)} 人，修正 {fixed} 個名字"}
    if has_summary:
        progress("寫回摘要")
        stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        scope = "摘要與逐字稿" if auth is not None else "只改摘要、不改逐字稿"
        section = (f"\n\n---\n\n{SECTION}\n\n"
                   f"以 SearXNG 搜尋結果與影片標題/說明自動比對（{stamp}，修正 {fixed} 個名字，{scope}）。\n\n"
                   + "\n".join(lines) + "\n")
        speakr("PUT", f"/recordings/{rid}/summary", auth, json={"summary": summary + section})
        result["summary_updated"] = True
    return result


def claim(rid: int) -> bool:
    """同一份錄音同時只跑一個查證（webhook 和按鈕共用）。"""
    with _busy_lock:
        if rid in _busy:
            return False
        _busy.add(rid)
        return True


def release(rid: int) -> None:
    with _busy_lock:
        _busy.discard(rid)


def run_async(rid: int) -> None:
    if not claim(rid):
        return

    def _job():
        try:
            log.info("recording %s 人名查證開始", rid)
            log.info("recording %s 人名查證完成：%s", rid, check_recording(rid)["message"])
        except Exception:  # noqa: BLE001
            log.exception("recording %s 人名查證失敗", rid)
        finally:
            release(rid)

    threading.Thread(target=_job, name=f"namecheck-{rid}", daemon=True).start()


# ---------------------------------------------------------------- webhook

def verify_signature(raw: bytes, header: str, window: int = 300) -> bool:
    import hashlib
    import hmac
    if not WEBHOOK_SECRET or not header:
        return False
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    try:
        t = int(next(v for k, v in parts if k == "t"))
    except (StopIteration, ValueError):
        return False
    if abs(time.time() - t) > window:
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in parts if k == "v1")


def handle_webhook(raw: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    if not enabled:
        return 503, {"error": "namecheck not configured"}
    if not verify_signature(raw, headers.get("speakr-signature-v2", "")):
        return 401, {"error": "bad signature"}
    event = headers.get("speakr-event", "")
    if event == "recording.summary.completed":
        rid = json.loads(raw).get("data", {}).get("recording_id")
        if isinstance(rid, int):
            run_async(rid)
    return 200, {"ok": True, "event": event}


if __name__ == "__main__":  # 手動：python namecheck.py <recording_id>
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not API_TOKEN:
        sys.exit("SPEAKR_API_TOKEN 沒設定")
    print(json.dumps(check_recording(int(sys.argv[1]), progress=print), ensure_ascii=False, indent=2))

"""一鍵匯入 Obsidian：把錄音的摘要、繁中翻譯、筆記、逐字稿寫成一篇 Markdown，放進 vault 的 Speakr 資料夾。

arch 上的 Obsidian（obsidian-server）會看到新檔案，LiveSync 再同步到手機、Mac 等其他裝置。

  POST /api/obsidian {"recording_id": 12, "overwrite": false}
    → 201 {"path": "Speakr/2026-09-23 標題.md"}
    → 409 {"exists": true, "edited": true/false, "path": ...}   已經匯入過（edited = 在 Obsidian 裡被改過）

權限：用使用者自己的 Speakr session 讀錄音。寫入範圍只有掛進容器的那一個資料夾。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

import translate

log = logging.getLogger("obsidian")

SPEAKR = os.getenv("SPEAKR_URL", "http://127.0.0.1:8899").rstrip("/")
OUT_DIR = Path(os.getenv("OBSIDIAN_DIR", "/obsidian"))        # 容器內路徑 = vault/<資料夾>
FOLDER = os.getenv("OBSIDIAN_FOLDER", "Speakr")               # 只用來顯示 vault 內的相對路徑
PUBLIC_URL = os.getenv("SPEAKR_PUBLIC_URL", "").rstrip("/")   # https://arch.<tailnet>.ts.net:8899
MAP_FILE = Path(os.getenv("TRANSLATE_DIR", "/data/translations")).parent / "obsidian-map.json"
TZ = ZoneInfo(os.getenv("TZ", "Asia/Taipei"))
_lock = threading.Lock()

enabled = OUT_DIR.is_dir()


def _get(path: str, auth: dict[str, str], **params) -> httpx.Response:
    r = httpx.get(f"{SPEAKR}/api/v1{path}", params=params, headers=auth, timeout=60, follow_redirects=False)
    r.raise_for_status()
    return r


def _safe_name(text: str, limit: int = 80) -> str:
    # exFAT / NTFS / Obsidian 連結都不接受的字元
    text = re.sub(r'[\\/:*?"<>|#^\[\]\x00-\x1f]', " ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].rstrip(" .") or "未命名錄音"


def _yaml_str(text: str) -> str:
    return json.dumps(text or "", ensure_ascii=False)   # JSON 字串也是合法的 YAML 字串


def _ts(sec) -> str:
    if sec is None:
        return ""
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def _date(rec: dict) -> str:
    for key in ("meeting_date", "created_at"):
        v = rec.get(key)
        if v:
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(TZ).strftime("%Y-%m-%d")
            except ValueError:
                return v[:10]
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _load_map() -> dict:
    try:
        return json.loads(MAP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_map(m: dict) -> None:
    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAP_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, MAP_FILE)


def build_note(rid: int, auth: dict[str, str]) -> tuple[str, str]:
    """回傳 (檔名, Markdown 內容)。"""
    rec = _get(f"/recordings/{rid}", auth, include="summary,notes").json()
    try:
        tr = _get(f"/recordings/{rid}/transcript", auth, format="json").json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        tr = {}
    title = rec.get("title") or f"錄音 {rid}"
    date = _date(rec)
    refs = [x.get("url") for x in rec.get("external_refs") or [] if x.get("url")]
    participants = [p.strip() for p in (rec.get("participants") or "").split(",") if p.strip()]
    tags = ["speakr"] + [re.sub(r"\s+", "-", t["name"]) for t in rec.get("tags") or [] if t.get("name")]
    trans = translate.load(rid)

    fm = ["---",
          f"title: {_yaml_str(title)}",
          f"date: {date}",
          f"speakr_id: {rid}"]
    if PUBLIC_URL:
        fm.append(f"speakr_url: {_yaml_str(f'{PUBLIC_URL}/recordings/{rid}')}")
    if refs:
        fm.append(f"source: {_yaml_str(refs[0])}")
    if rec.get("audio_duration"):
        fm.append(f"duration: {_yaml_str(_ts(rec['audio_duration']))}")
    fm.append("participants: [" + ", ".join(_yaml_str(p) for p in participants) + "]")
    fm.append("tags: [" + ", ".join(_yaml_str(t) for t in tags) + "]")
    fm.append(f"imported: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}")
    fm.append("---")

    body = [f"# {title}", ""]
    links = []
    if PUBLIC_URL:
        links.append(f"[在 Speakr 開啟]({PUBLIC_URL}/recordings/{rid})")
    if refs:
        links.append(f"[原始影片]({refs[0]})")
    if links:
        body += [" · ".join(links), ""]

    summary = (rec.get("summary") or "").strip()
    if summary and not summary.startswith("[Summary not generated"):
        body += ["## 摘要", "", _demote(summary), ""]
    if trans.get("summary", {}).get("text"):
        body += ["## 摘要（繁中翻譯）", "", _demote(trans["summary"]["text"]), ""]
    notes = (rec.get("notes") or "").strip()
    if notes:
        body += ["## 筆記", "", notes, ""]

    segs = tr.get("segments") or []
    tmap = {}
    if trans.get("transcript", {}).get("segments"):
        tmap = {i: s.get("translation", "") for i, s in enumerate(trans["transcript"]["segments"])}
        if len(tmap) != len(segs):  # 逐字稿在翻譯後改過，對不上就不附譯文
            tmap = {}
    if segs:
        body += ["## 逐字稿" + ("（附繁中翻譯）" if tmap else ""), ""]
        last = None
        for i, s in enumerate(segs):
            text = (s.get("text") or s.get("sentence") or "").strip()
            if not text:
                continue
            spk = (s.get("speaker") or "").strip()
            head = f"**[{_ts(s.get('start_time'))}] {spk}**" if spk != last else f"[{_ts(s.get('start_time'))}]"
            last = spk
            body.append(f"{head} {text}")
            if tmap.get(i):
                body.append(f"> {tmap[i]}")
            body.append("")
    elif tr.get("raw"):
        body += ["## 逐字稿", "", tr["raw"].strip(), ""]

    name = f"{date} {_safe_name(title)}.md"
    return name, "\n".join(fm) + "\n\n" + "\n".join(body).rstrip() + "\n"


def _demote(md: str) -> str:
    """摘要裡的 # / ## 標題往下降兩級，才不會跟筆記本身的 # 標題、## 段落搶層級。"""
    return re.sub(r"^(#{1,4}) ", lambda m: "#" * min(6, len(m.group(1)) + 2) + " ", md, flags=re.M)


def export(rid: int, auth: dict[str, str], overwrite: bool) -> tuple[int, dict]:
    if not OUT_DIR.is_dir():
        return 503, {"error": "還沒設定 Obsidian 匯出，或外接硬碟沒掛上（跑 ./scripts/install-obsidian-export.sh）"}
    name, content = build_note(rid, auth)
    with _lock:
        m = _load_map()
        prev = m.get(str(rid))
        target = OUT_DIR / (prev["file"] if prev and (OUT_DIR / prev["file"]).exists() else name)
        if target.exists() and not overwrite:
            edited = bool(prev) and abs(target.stat().st_mtime - prev.get("mtime", 0)) > 2
            return 409, {"exists": True, "edited": edited, "path": f"{FOLDER}/{target.name}"}
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
        m[str(rid)] = {"file": target.name, "mtime": target.stat().st_mtime}
        _save_map(m)
    log.info("recording %s → %s/%s（%d 字）", rid, FOLDER, target.name, len(content))
    return 201, {"path": f"{FOLDER}/{target.name}", "chars": len(content)}

"""speakr-url：把影片/音訊網址交給 yt-dlp 下載音訊，再用 Speakr 的上傳 API 送進轉錄流程。

  POST /api/import        {"url": "...", "transcription_model": ""}  → {"id": ..., "state": "queued"}
  GET  /api/jobs/<id>     → 工作狀態
  POST /api/namecheck     {"recording_id": 123}  → 人名查證工作（逐字稿工具列「校正姓名」按鈕）
  GET  /api/translation/<rid>                   → 繁中翻譯（右側「繁中翻譯」分頁）
  POST /api/translation   {"recording_id": 123}  → 開始翻譯摘要＋逐字稿
  POST /api/obsidian      {"recording_id": 123, "overwrite": false} → 寫進 Obsidian vault 的 Speakr 資料夾
  GET  /api/health

驗證：沿用使用者在 Speakr 的登入 session（瀏覽器同源請求會帶 cookie），
本服務先拿 cookie 打 Speakr 的 /api/v1/users/me 確認身分，上傳也用同一個 session，
所以錄音會歸在那位使用者名下，不需要另外的 API token。

只綁 127.0.0.1；對外由 tailscale serve 以 /url-import 路徑掛在 Speakr 同一個網址底下。
只用標準函式庫 + httpx；yt-dlp 以子行程執行（和 Speakr 的 Python 環境隔離）。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

import namecheck
import translate
import obsidian_export

log = logging.getLogger("speakr-url")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # 不要每次輪詢都記一行

SPEAKR = os.getenv("SPEAKR_URL", "http://127.0.0.1:8899").rstrip("/")
HOST, PORT = os.getenv("LISTEN", "127.0.0.1:8897").rsplit(":", 1)
PREFIX = "/url-import"
YTDLP = os.getenv("YTDLP_BIN", shutil.which("yt-dlp") or "yt-dlp")
FFMPEG = os.getenv("FFMPEG_BIN", shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg")
COOKIES = os.getenv("YTDLP_COOKIES", "")  # 選用：Netscape cookies.txt（年齡限制、會員影片）
MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", "14400"))  # 4 小時
BITRATE = os.getenv("AUDIO_BITRATE", "96K")  # 語音 96k AAC 綽綽有餘；4 小時約 170 MB < Speakr 預設 250 MB
CONCURRENCY = int(os.getenv("CONCURRENCY", "2"))
ALLOW_PRIVATE = os.getenv("ALLOW_PRIVATE_URLS", "0") == "1"
WORK = Path(os.getenv("WORK_DIR", tempfile.gettempdir())) / "speakr-url"
JOB_TTL = 24 * 3600

FORWARD_HEADERS = ("cookie", "user-agent", "x-forwarded-for", "accept-language")

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
slots = threading.Semaphore(CONCURRENCY)


# ---------------------------------------------------------------- 工具

class ImportError_(Exception):
    """給使用者看的錯誤訊息。"""


def speakr_headers(src: dict[str, str]) -> dict[str, str]:
    h = {k: v for k, v in src.items() if k in FORWARD_HEADERS}
    # 讓 Speakr 認為這是經 HTTPS 代理進來的同一個瀏覽器（session cookie 是 Secure 的）
    h["x-forwarded-proto"] = "https"
    return h


_who_cache: dict[str, tuple[float, dict]] = {}
_who_lock = threading.Lock()


def whoami(headers: dict[str, str]) -> dict | None:
    """同一個 cookie 30 秒內不重複問 Speakr（前端每 2 秒輪詢一次）。"""
    cookie = headers.get("cookie")
    if not cookie:
        return None
    now = time.time()
    with _who_lock:
        hit = _who_cache.get(cookie)
        if hit and now - hit[0] < 30:
            return hit[1]
    user = _whoami_remote(headers)
    if user:
        with _who_lock:
            if len(_who_cache) > 256:
                _who_cache.clear()
            _who_cache[cookie] = (now, user)
    return user


def _whoami_remote(headers: dict[str, str]) -> dict | None:
    try:
        r = httpx.get(f"{SPEAKR}/api/v1/users/me", headers=speakr_headers(headers), timeout=10, follow_redirects=False)
    except httpx.HTTPError as exc:
        log.warning("連不到 Speakr：%s", exc)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    user = data.get("user", data) if isinstance(data, dict) else {}
    return user if user.get("id") is not None else None


def check_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ImportError_("只接受 http / https 網址")
    if not ALLOW_PRIVATE:
        try:
            infos = socket.getaddrinfo(parts.hostname, None)
        except socket.gaierror:
            raise ImportError_(f"解析不到主機 {parts.hostname}")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ImportError_("不接受內網 / 本機位址")
    return url


def ytdlp_base() -> list[str]:
    cmd = [YTDLP, "--no-playlist", "--no-warnings", "--ignore-config", "--ffmpeg-location", FFMPEG]
    if COOKIES and Path(COOKIES).is_file():
        cmd += ["--cookies", COOKIES]
    return cmd


def last_error(stderr: str) -> str:
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    msg = next((ln for ln in reversed(lines) if "ERROR" in ln), lines[-1] if lines else "yt-dlp 失敗")
    return re.sub(r"^ERROR:\s*", "", msg)[:300]


def update(job_id: str, **fields) -> None:
    with jobs_lock:
        jobs[job_id].update(fields, updated=time.time())


def public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in ("user_id", "headers")}


# ---------------------------------------------------------------- 工作流程

def run_job(job_id: str) -> None:
    with jobs_lock:
        job = dict(jobs[job_id])
    workdir = WORK / job_id
    with slots:
        try:
            workdir.mkdir(parents=True, exist_ok=True)

            # 1) 讀影片資訊
            update(job_id, state="probing")
            p = subprocess.run(ytdlp_base() + ["-J", "--", job["url"]], capture_output=True, text=True, timeout=180)
            if p.returncode != 0:
                raise ImportError_(last_error(p.stderr))
            info = json.loads(p.stdout)
            if info.get("_type") in ("playlist", "multi_video"):
                raise ImportError_("這是播放清單，請貼單一影片的網址")
            if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
                raise ImportError_("直播中或尚未開播的影片無法匯入")
            duration = info.get("duration") or 0
            if duration and duration > MAX_DURATION:
                raise ImportError_(f"影片長度 {duration / 3600:.1f} 小時，超過上限 {MAX_DURATION / 3600:.1f} 小時")
            title = (info.get("title") or "未命名影片").strip()
            update(job_id, title=title, duration=duration)

            # 2) 只下載音訊 → m4a
            update(job_id, state="downloading", progress=0.0)
            # 優先直接抓 m4a（YouTube 的 AAC 音軌），這樣 -x 只是換容器、不必重新編碼，下載完幾乎立刻結束
            cmd = ytdlp_base() + [
                "-f", "bestaudio[ext=m4a]/bestaudio/best", "-x", "--audio-format", "m4a", "--audio-quality", BITRATE,
                "-o", str(workdir / "%(id)s.%(ext)s"),
                "--newline", "--progress-template", "download:PROGRESS %(progress._percent_str)s",
                "--", job["url"],
            ]
            # stderr 寫到檔案：用 PIPE 又不讀的話，輸出一多子行程會卡死
            errlog = workdir / "yt-dlp.err"
            with errlog.open("w") as errfh:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfh, text=True)
                deadline = time.time() + 3 * 3600
                assert proc.stdout is not None
                for line in proc.stdout:
                    m = re.search(r"PROGRESS\s+([\d.]+)%", line)
                    if m:
                        update(job_id, progress=min(float(m.group(1)), 100.0))
                    elif line.startswith(("[ExtractAudio]", "[FixupM4a]", "[Fixup", "[ffmpeg]")):
                        update(job_id, state="converting")
                    if time.time() > deadline:
                        proc.kill()
                        raise ImportError_("下載超過 3 小時，已中止")
                proc.wait()
            if proc.returncode != 0:
                raise ImportError_(last_error(errlog.read_text(errors="replace")))
            files = sorted(p for p in workdir.glob("*.m4a") if p.stat().st_size > 0)
            if not files:
                raise ImportError_("下載完成但找不到音訊檔")
            audio = files[0]

            # 3) 交給 Speakr
            update(job_id, state="uploading", progress=100.0, size_mb=round(audio.stat().st_size / 1e6, 1))
            log.info("%s 下載完成 %.1f MB，上傳到 Speakr", job_id, audio.stat().st_size / 1e6)
            webpage = info.get("webpage_url") or job["url"]
            notes = [f"來源：{webpage}"]
            if info.get("uploader") or info.get("channel"):
                notes.append(f"上傳者：{info.get('uploader') or info.get('channel')}")
            description = (info.get("description") or "").strip()
            if description:
                notes.append(f"\n影片說明：\n{description[:3000]}")
            data = {
                "title": title[:250],
                "notes": "\n".join(notes),
                "external_refs": json.dumps([{
                    "system": "yt-dlp",
                    "kind": (info.get("extractor_key") or "video").lower(),
                    "ref": str(info.get("id") or webpage),
                    "url": webpage,
                    "label": title[:200],
                }], ensure_ascii=False),
                "idempotency_key": f"ytdlp:{info.get('extractor_key', 'x')}:{info.get('id', '')}"[:100],
            }
            if info.get("upload_date") and re.fullmatch(r"\d{8}", info["upload_date"]):
                d = info["upload_date"]
                data["meeting_date"] = f"{d[:4]}-{d[4:6]}-{d[6:]}T00:00:00"
            # 標題 + 說明開頭當成轉錄提示：人名、專有名詞照影片的寫法（Whisper 類模型只看約 200 token）
            hint = re.sub(r"https?://\S+|#\S+", " ", f"{title}。{description[:300]}")
            hint = re.sub(r"\s+", " ", hint).strip()
            if hint:
                data["initial_prompt"] = hint[:400]
            if job.get("transcription_model"):
                data["transcription_model"] = job["transcription_model"]
            with audio.open("rb") as fh:
                r = httpx.post(
                    f"{SPEAKR}/api/v1/recordings/upload",
                    headers=speakr_headers(job["headers"]),
                    data=data,
                    files={"file": (f"{title[:80]}.m4a", fh, "audio/mp4")},
                    timeout=httpx.Timeout(connect=10, read=600, write=600, pool=10),
                )
            try:
                body = r.json()
            except ValueError:
                body = {}
            if r.status_code not in (200, 201, 202):
                raise ImportError_(f"Speakr 拒絕上傳（HTTP {r.status_code}）：{body.get('error') or r.text[:200]}")
            update(job_id, state="done", recording_id=body.get("id"), duplicate=bool(body.get("idempotent_replay")))
            log.info("完成 %s → recording %s（%s）", job_id, body.get("id"), title)

        except ImportError_ as exc:
            update(job_id, state="error", error=str(exc))
            log.info("失敗 %s：%s", job_id, exc)
        except subprocess.TimeoutExpired:
            update(job_id, state="error", error="讀取影片資訊逾時")
        except Exception as exc:  # noqa: BLE001
            log.exception("工作 %s 發生未預期錯誤", job_id)
            update(job_id, state="error", error=f"內部錯誤：{type(exc).__name__}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            with jobs_lock:  # 憑證用完就丟
                jobs[job_id].pop("headers", None)


def run_namecheck(job_id: str, rid: int, headers: dict[str, str]) -> None:
    """按鈕觸發的人名查證：以使用者本人的 session 讀錄音、改摘要；逐字稿的修正清單回給瀏覽器套用。"""
    try:
        update(job_id, state="running", step="開始")
        result = namecheck.check_recording(rid, auth=speakr_headers(headers),
                                           progress=lambda msg: update(job_id, step=msg))
        update(job_id, state="done", step="", **result)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        msg = {401: "登入逾時，請重新整理頁面", 403: "你沒有這份錄音的編輯權限", 404: "找不到這份錄音"}.get(code, f"Speakr 回應 HTTP {code}")
        update(job_id, state="error", error=msg)
    except httpx.HTTPError as exc:
        log.warning("namecheck %s 連線失敗：%s", rid, exc)
        update(job_id, state="error", error="連不到 Ollama / SearXNG / Speakr（看 journalctl --user -u speakr-url）")
    except Exception:  # noqa: BLE001
        log.exception("namecheck %s 失敗", rid)
        update(job_id, state="error", error="查證失敗（看 journalctl --user -u speakr-url）")
    finally:
        namecheck.release(rid)
        with jobs_lock:
            jobs[job_id].pop("headers", None)


def janitor() -> None:
    while True:
        time.sleep(600)
        cutoff = time.time() - JOB_TTL
        with jobs_lock:
            for jid in [j for j, v in jobs.items() if v.get("updated", 0) < cutoff]:
                jobs.pop(jid, None)
        translate.gc_jobs()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "speakr-url/1.0"

    def log_message(self, fmt, *args):  # 交給 logging，且不記錄網址參數
        log.debug("%s %s", self.command, self.path.split("?")[0])

    def _path(self) -> str:
        path = self.path.split("?", 1)[0]
        if path.startswith(PREFIX):  # tailscale serve 有沒有去掉前綴都能用
            path = path[len(PREFIX):] or "/"
        return path

    def _headers(self) -> dict[str, str]:
        return {k.lower(): v for k, v in self.headers.items()}

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self._path()
        if path == "/api/health":
            return self._send(200, {"status": "ok", "ytdlp": YTDLP_VERSION, "namecheck": namecheck.enabled,
                                    "obsidian": obsidian_export.OUT_DIR.is_dir()})
        m = re.fullmatch(r"/api/translation/(\d{1,12})", path)
        if m:
            headers = self._headers()
            if not whoami(headers):
                return self._send(401, {"error": "not logged in"})
            rid = int(m.group(1))
            try:
                rec = translate.fetch_recording(rid, speakr_headers(headers))
            except httpx.HTTPStatusError as exc:
                return self._send(exc.response.status_code if exc.response.status_code in (403, 404) else 502,
                                  {"error": "無法讀取這份錄音"})
            except httpx.HTTPError:
                return self._send(502, {"error": "連不到 Speakr"})
            return self._send(200, translate.view(rid, rec))
        m = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]{8,64})", path)
        if m:
            user = whoami(self._headers())
            if not user:
                return self._send(401, {"error": "not logged in"})
            with jobs_lock:
                job = jobs.get(m.group(1))
                if not job or job.get("user_id") != user["id"]:
                    return self._send(404, {"error": "not found"})
                return self._send(200, public(job))
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self._path() == "/hooks/speakr":  # Speakr webhook → 人名查證（以 HMAC 簽章驗證，不靠 cookie）
            length = int(self.headers.get("content-length", "0") or 0)
            raw = self.rfile.read(min(length, 1 << 20))
            status, payload = namecheck.handle_webhook(raw, self._headers())
            return self._send(status, payload)
        if self._path() not in ("/api/import", "/api/namecheck", "/api/translation", "/api/obsidian"):
            return self._send(404, {"error": "not found"})
        headers = self._headers()
        # 擋跨站請求（CSRF）：要求 JSON + 自訂標頭。別的網站要送這種請求必須先過 CORS preflight，
        # 而本服務不回應 OPTIONS，所以瀏覽器會擋下。不依賴 Host / Origin，tailscale serve 怎麼改標頭都不影響。
        if not headers.get("content-type", "").startswith("application/json") or headers.get("x-speakr-url") != "1":
            return self._send(403, {"error": "forbidden"})
        user = whoami(headers)
        if not user:
            return self._send(401, {"error": "not logged in"})
        if self._path() == "/api/namecheck":
            return self._namecheck(headers, user)
        if self._path() == "/api/obsidian":
            try:
                length = int(headers.get("content-length", "0"))
                req = json.loads(self.rfile.read(min(length, 4096)) or b"{}")
                rid = int(req.get("recording_id"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return self._send(400, {"error": "bad request"})
            try:
                status, payload = obsidian_export.export(rid, speakr_headers(headers), bool(req.get("overwrite")))
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                return self._send(code if code in (401, 403, 404) else 502, {"error": "無法讀取這份錄音"})
            except httpx.HTTPError:
                return self._send(502, {"error": "連不到 Speakr"})
            except OSError as exc:
                log.warning("寫入 Obsidian 失敗：%s", exc)
                return self._send(500, {"error": f"寫不進 vault：{exc.strerror or exc}"})
            log.info("Obsidian 匯出 recording %s（user %s）→ %s", rid, user["id"], status)
            return self._send(status, payload)
        if self._path() == "/api/translation":
            try:
                length = int(headers.get("content-length", "0"))
                rid = int(json.loads(self.rfile.read(min(length, 4096)) or b"{}").get("recording_id"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return self._send(400, {"error": "bad request"})
            auth = speakr_headers(headers)
            try:
                translate.fetch_recording(rid, auth, use_cache=False)  # 權限檢查
            except httpx.HTTPStatusError as exc:
                return self._send(exc.response.status_code if exc.response.status_code in (403, 404) else 502,
                                  {"error": "無法讀取這份錄音"})
            except httpx.HTTPError:
                return self._send(502, {"error": "連不到 Speakr"})
            status, payload = translate.start(rid, auth, user["id"])
            log.info("翻譯 recording %s（user %s）→ %s", rid, user["id"], status)
            return self._send(status, payload)
        try:
            length = int(headers.get("content-length", "0"))
            req = json.loads(self.rfile.read(min(length, 16384)) or b"{}")
            url = check_url(str(req.get("url", "")))
        except ImportError_ as exc:
            return self._send(400, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "bad request"})
        model = str(req.get("transcription_model") or "").strip()[:100]
        job_id = secrets.token_urlsafe(12)
        job = {
            "id": job_id, "url": url, "state": "queued", "progress": None, "title": "",
            "created": datetime.now(timezone.utc).isoformat(), "updated": time.time(),
            "transcription_model": model, "user_id": user["id"],
            "headers": {k: v for k, v in headers.items() if k in FORWARD_HEADERS},
        }
        with jobs_lock:
            jobs[job_id] = job
        threading.Thread(target=run_job, args=(job_id,), name=f"job-{job_id}", daemon=True).start()
        log.info("新工作 %s（user %s）", job_id, user["id"])
        return self._send(202, public(job))


    def _namecheck(self, headers: dict[str, str], user: dict):
        try:
            length = int(headers.get("content-length", "0"))
            rid = int(json.loads(self.rfile.read(min(length, 4096)) or b"{}").get("recording_id"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "bad request"})
        if not namecheck.claim(rid):
            return self._send(409, {"error": "這份錄音正在查證中，請稍候"})
        job_id = secrets.token_urlsafe(12)
        job = {
            "id": job_id, "kind": "namecheck", "recording_id": rid, "state": "queued", "step": "",
            "created": datetime.now(timezone.utc).isoformat(), "updated": time.time(), "user_id": user["id"],
            "headers": {k: v for k, v in headers.items() if k in FORWARD_HEADERS},
        }
        with jobs_lock:
            jobs[job_id] = job
        threading.Thread(target=run_namecheck, args=(job_id, rid, job["headers"]),
                         name=f"namecheck-{rid}", daemon=True).start()
        log.info("人名查證 %s（recording %s，user %s）", job_id, rid, user["id"])
        return self._send(202, public(job))


def _ytdlp_version() -> str:
    try:
        return subprocess.run([YTDLP, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


YTDLP_VERSION = _ytdlp_version()

if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=janitor, daemon=True).start()
    log.info("listening on %s:%s → Speakr %s，yt-dlp %s，ffmpeg %s", HOST, PORT, SPEAKR, YTDLP_VERSION, FFMPEG)
    ThreadingHTTPServer((HOST, int(PORT)), Handler).serve_forever()

"""從伺服器資料夾選檔匯入：瀏覽唯讀掛進容器的資料夾（預設整個 /mnt），選影片／音訊後由伺服器直接抽出音訊再交給 Speakr。

  GET  /api/browse?path=<相對路徑>         → 資料夾與媒體檔清單
  POST /api/import-local {"path": "..."}   → 工作（和網址匯入共用 /api/jobs/<id> 與右下角進度）

為什麼不用瀏覽器上傳：影片動輒幾 GB，經瀏覽器上傳會撞到 Speakr 的上傳上限、又慢；
這裡在伺服器上用 ffmpeg 只取音訊（96 kbps 單聲道，2 小時約 85 MB），再用內網上傳 API 送進去，檔案大小不再是問題。

安全：
  - 只能看 LOCAL_IMPORT_DIR 底下（realpath 檢查，符號連結指到外面的一律拒絕）。
  - 預設只有 Speakr 管理員能用（LOCAL_IMPORT_ADMIN_ONLY=0 才開放所有登入的使用者）。
  - 容器是唯讀掛載，不會改到原檔。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.getenv("LOCAL_IMPORT_DIR", "/import"))
ADMIN_ONLY = os.getenv("LOCAL_IMPORT_ADMIN_ONLY", "1") != "0"
MAX_ENTRIES = int(os.getenv("LOCAL_IMPORT_MAX_ENTRIES", "3000"))
# 單檔長度上限（秒）。預設 6 小時：96 kbps 約 260 MB，仍在 Speakr 單次上傳的 1000 MB 內
MAX_DURATION = int(os.getenv("LOCAL_MAX_DURATION_SECONDS", "21600"))

enabled = ROOT.is_dir()

MEDIA_EXT = {
    # 音訊
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus", ".wma", ".amr", ".aiff", ".aif", ".caf", ".mka",
    # 影片
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv", ".ts", ".mts", ".m2ts", ".3gp", ".mpg", ".mpeg", ".ogv",
}
HIDDEN = {"lost+found", "$RECYCLE.BIN", "System Volume Information", "@eaDir", ".Trash-1000"}


class PathError(Exception):
    """給使用者看的錯誤訊息。"""


def resolve(rel: str) -> Path:
    """相對路徑 → 容器內的真實路徑；跑到 ROOT 外面（.. 或符號連結）就拒絕。"""
    if "\x00" in rel:
        raise PathError("路徑不合法")
    root = ROOT.resolve()
    p = (root / rel.lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise PathError("路徑不在允許的資料夾內")
    return p


def rel_of(p: Path) -> str:
    root = ROOT.resolve()
    return "" if p == root else p.relative_to(root).as_posix()


def is_media(p: Path) -> bool:
    return p.suffix.lower() in MEDIA_EXT


def browse(rel: str) -> dict:
    if not enabled:
        raise PathError("還沒設定資料夾匯入（跑 ./scripts/install-local-files.sh）")
    d = resolve(rel)
    if not d.is_dir():
        raise PathError("找不到這個資料夾（外接硬碟有掛上嗎？）")
    dirs, files, truncated = [], [], False
    try:
        with os.scandir(d) as it:
            for n, e in enumerate(it):
                if n >= MAX_ENTRIES:
                    truncated = True
                    break
                name = e.name
                if name.startswith(".") or name in HIDDEN:
                    continue
                try:
                    st = e.stat()  # 跟著符號連結走；壞掉的連結會丟 OSError
                    real = Path(e.path).resolve()
                except OSError:
                    continue
                root = ROOT.resolve()
                if real != root and root not in real.parents:
                    continue  # 符號連結指到允許範圍外：看不到
                if e.is_dir():
                    dirs.append({"name": name, "type": "dir", "mtime": int(st.st_mtime)})
                elif e.is_file() and is_media(real):
                    files.append({"name": name, "type": "file", "size": st.st_size, "mtime": int(st.st_mtime)})
    except PermissionError:
        raise PathError("沒有讀取這個資料夾的權限")
    key = lambda x: x["name"].casefold()  # noqa: E731
    cur = rel_of(d)
    parent = None if not cur else rel_of(d.parent)
    return {"root": str(ROOT), "path": cur, "parent": parent, "truncated": truncated,
            "entries": sorted(dirs, key=key) + sorted(files, key=key)}

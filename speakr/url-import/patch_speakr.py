"""在 Speakr 的上傳視窗插入「從網址匯入」欄位、在逐字稿工具列加「校正姓名」按鈕，並載入 url-import.js。

建置映像時執行一次。錨點找不到（Speakr 改版改了模板）就讓 build 失敗，
而不是默默做出一個沒有欄位的映像。
"""
import sys
from pathlib import Path

APP = Path(sys.argv[1] if len(sys.argv) > 1 else "/app")
MARK = "speakr-url-import"

UPLOAD_VIEW = APP / "templates/components/upload-view.html"
UPLOAD_ANCHOR = "            <!-- Audio Recording Options (hidden when files are selected) -->"
# 注意：這段在 Jinja + Vue 模板裡，不能出現雙大括號，也不能放 <script>
URL_BLOCK = """            <!-- speakr-url-import: 從網址匯入（yt-dlp） -->
            <div v-if="pendingQueueFiles.length === 0" id="speakr-url-import" class="mb-4 space-y-2">
                <h3 class="text-xs font-medium text-[var(--text-secondary)] text-center">從網址匯入（YouTube 等，yt-dlp）</h3>
                <div style="display:flex; gap:0.5rem;">
                    <input id="speakr-url-input" type="url" inputmode="url" autocomplete="off"
                           placeholder="https://www.youtube.com/watch?v=..."
                           style="flex:1; min-width:0; padding:0.5rem 0.75rem; border-radius:0.5rem; font-size:0.875rem;
                                  background: var(--bg-input, var(--bg-secondary)); color: var(--text-primary);
                                  border: 1px solid var(--border-secondary);">
                    <button type="button" id="speakr-url-import-btn"
                            style="padding:0.5rem 1rem; border-radius:0.5rem; font-size:0.875rem; font-weight:500;
                                   background: var(--bg-accent, #2563eb); color: #fff; border: none; white-space: nowrap;">
                        下載並轉錄
                    </button>
                </div>
                <p id="speakr-url-hint" class="text-[10px] text-[var(--text-muted)] text-center">
                    只抓音訊，下載完自動加入轉錄佇列；進度顯示在右下角。
                </p>
            </div>

"""

# 逐字稿工具列（桌面版）：放在 Bubble 切換鈕之後、複製鈕之前，樣式和 Bubble 一樣（class="toggle"）
DESKTOP_TOOLBAR = APP / "templates/components/detail/desktop-transcription-panel.html"
DESKTOP_ANCHOR = "            <!-- Copy -->"
DESKTOP_BTN = """            <!-- speakr-url-import: 校正姓名（上網查證人名，修正摘要與逐字稿） -->
            <button v-if="selectedRecording && selectedRecording.transcription"
                    type="button"
                    class="toggle speakr-namecheck-btn"
                    :data-rid="selectedRecording.id"
                    title="上網查證人名，修正摘要與逐字稿裡的錯字">
                <i class="fas fa-user-check" style="font-size: 11px;"></i>
                <span>校正姓名</span>
            </button>

"""

# 手機版標題列（逐字稿分頁）：只有圖示，和旁邊的按鈕一致
MOBILE_TOOLBAR = APP / "templates/components/detail/mobile-header.html"
MOBILE_ANCHOR = '<button @click.stop="copyTranscription" class="btn btn--icon btn--sm"'
MOBILE_BTN = """                <button v-if="selectedRecording.transcription" @click.stop type="button" class="btn btn--icon btn--sm speakr-namecheck-btn" :data-rid="selectedRecording.id" title="校正姓名"><i class="fas fa-user-check"></i></button><!-- speakr-url-import -->
"""

# 右側分頁（桌面版）：Summary / Notes / Stats 後面加「繁中翻譯」分頁，內容由 url-import.js 繪製
RIGHT_PANEL = APP / "templates/components/detail/desktop-right-panel.html"
TAB_ANCHOR = '                <button v-if="selectedRecording.events && selectedRecording.events.length > 0"'
TAB_MARK = "speakr-url-import-translation-tab"
TAB_BTN = """                <!-- speakr-url-import-translation-tab -->
                <button v-if="selectedRecording && selectedRecording.id"
                        @click="selectedTab = 'translation'"
                        class="tab"
                        :class="{'is-active': selectedTab === 'translation'}">
                    <span>繁中翻譯</span>
                </button>
"""
PANEL_ANCHOR = "{% include 'components/detail/desktop-events-tab.html' %}"
PANEL_MARK = "speakr-url-import-translation-panel"
PANEL = """            <!-- speakr-url-import-translation-panel：內容由 url-import.js 繪製，Vue 不管裡面的節點 -->
            <div v-if="selectedTab === 'translation'" class="h-full overflow-y-auto px-4 py-4 speakr-tr-panel"
                 :data-rid="selectedRecording.id"></div>
"""

# 手機版：摘要分頁標題列加一個「翻譯」圖示，開啟全螢幕的翻譯檢視
MOBILE_TR_ANCHOR = '<button @click.stop="toggleEditSummary" class="btn btn--icon btn--sm"'
MOBILE_TR_MARK = "speakr-url-import-translation-mobile"
MOBILE_TR_BTN = """                <button v-if="selectedRecording.id" @click.stop type="button" class="btn btn--icon btn--sm speakr-tr-open" :data-rid="selectedRecording.id" title="繁中翻譯"><i class="fas fa-language"></i></button><!-- speakr-url-import-translation-mobile -->
"""

# 逐字稿面板（桌面版左側）：「Transcription」旁邊加「繁中翻譯」分頁，切換時用翻譯取代原本的逐字稿區塊
LEFT_TAB_ANCHOR = "<span v-text=\"t('transcription.title')\"></span>"
LEFT_TAB_MARK = "speakr-url-import-left-tab"
LEFT_TAB = """            <!-- speakr-url-import-left-tab -->
            <button v-if="selectedRecording && selectedRecording.id" type="button" class="tab speakr-tr-left-tab"
                    :data-rid="selectedRecording.id">
                <i class="fas fa-language" style="font-size: 13px; opacity: 0.7;"></i>
                <span>繁中翻譯</span>
            </button>
"""
LEFT_PANEL_ANCHOR = "    <!-- Transcription Content -->"
LEFT_PANEL_MARK = "speakr-url-import-left-panel"
LEFT_PANEL = """    <!-- speakr-url-import-left-panel：翻譯模式時顯示，並隱藏下一個區塊（原本的逐字稿）；內容由 url-import.js 繪製 -->
    <div class="speakr-tr-left flex-1 overflow-y-auto px-4 py-4" :data-rid="selectedRecording ? selectedRecording.id : ''"></div>
"""

# 錄音標題列：分享鈕旁邊加「匯入 Obsidian」（桌面版、手機版）
HEADER = APP / "templates/components/header.html"
HEADER_ANCHOR = "            <!-- More menu: the destructive actions, kept one click further away. -->"
HEADER_MARK = "speakr-url-import-obsidian"
HEADER_BTN = """            <!-- speakr-url-import-obsidian -->
            <button v-if="selectedRecording && selectedRecording.id" type="button"
                    class="btn btn--icon hidden md:inline-flex speakr-obs-btn"
                    :data-rid="selectedRecording.id" title="匯入 Obsidian">
                <i class="fas fa-gem"></i>
            </button>
"""
MOBILE_OBS_ANCHOR = "                    <!-- Pushes delete to the right edge so it's"
MOBILE_OBS_MARK = "speakr-url-import-obsidian-mobile"
MOBILE_OBS_BTN = """                    <button v-if="selectedRecording.id" @click.stop type="button" class="btn btn--icon btn--sm flex-shrink-0 speakr-obs-btn" :data-rid="selectedRecording.id" title="匯入 Obsidian"><i class="fas fa-gem"></i></button><!-- speakr-url-import-obsidian-mobile -->
"""

INDEX = APP / "templates/index.html"
INDEX_ANCHOR = "filename='js/csrf-refresh.js'"
SCRIPT_TAG = '    <script src="/static/js/url-import.js" defer></script><!-- speakr-url-import -->\n'


def patch(path: Path, anchor: str, insert: str, before: bool, mark: str = MARK, offset: int = 0) -> None:
    """mark：判斷這一處是否已插入過的字串（同一個檔案插多處時要各自不同）。"""
    text = path.read_text(encoding="utf-8")
    if mark in text:
        print(f"{path}: 已經打過補丁（{mark}），略過")
        return
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if anchor in line]
    if len(hits) != 1:
        sys.exit(f"{path}: 錨點出現 {len(hits)} 次（需要剛好 1 次）：{anchor!r} — Speakr 模板可能改版了")
    i = (hits[0] if before else hits[0] + 1) + offset
    lines.insert(i, insert)
    path.write_text("".join(lines), encoding="utf-8")
    print(f"{path}: 已插入")


patch(UPLOAD_VIEW, UPLOAD_ANCHOR, URL_BLOCK, before=True)
patch(DESKTOP_TOOLBAR, DESKTOP_ANCHOR, DESKTOP_BTN, before=True)
patch(MOBILE_TOOLBAR, MOBILE_ANCHOR, MOBILE_BTN, before=True)
patch(RIGHT_PANEL, TAB_ANCHOR, TAB_BTN, before=True, mark=TAB_MARK)
patch(RIGHT_PANEL, PANEL_ANCHOR, PANEL, before=True, mark=PANEL_MARK)
patch(MOBILE_TOOLBAR, MOBILE_TR_ANCHOR, MOBILE_TR_BTN, before=True, mark=MOBILE_TR_MARK)
# 錨點是 Transcription 標籤文字那一行，插在它下一行的 </span> 之後（offset=1）
patch(DESKTOP_TOOLBAR, LEFT_TAB_ANCHOR, LEFT_TAB, before=False, mark=LEFT_TAB_MARK, offset=1)
patch(DESKTOP_TOOLBAR, LEFT_PANEL_ANCHOR, LEFT_PANEL, before=True, mark=LEFT_PANEL_MARK)
patch(HEADER, HEADER_ANCHOR, HEADER_BTN, before=True, mark=HEADER_MARK)
patch(MOBILE_TOOLBAR, MOBILE_OBS_ANCHOR, MOBILE_OBS_BTN, before=True, mark=MOBILE_OBS_MARK)
patch(INDEX, INDEX_ANCHOR, SCRIPT_TAG, before=False)

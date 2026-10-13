# Speakr — 錄音整理 / 會議筆記（接 asr-server + Ollama，經 Tailscale 存取）

```
手機 / 電腦瀏覽器（tailnet）
  └─ https://arch.<tailnet>.ts.net:8899   ← tailscale serve（自動 HTTPS 憑證）
        └─ 127.0.0.1:8899  speakr（Quadlet，Network=host）
              ├─ 轉錄 → 127.0.0.1:8098/v1   asr-server（Breeze-ASR-25，3090）
              └─ 摘要 / 標題 / 對話 → 127.0.0.1:11435/v1   ollama-metrics-proxy → Ollama
```

- 錄音、逐字稿、筆記全部存在主機的 podman volume（`speakr-uploads`、`speakr-instance`），不會傳到外部服務。
- 只綁 `127.0.0.1`，LAN 和網際網路都連不到；只有 tailnet 裡的裝置能用。
- 有 HTTPS，所以手機瀏覽器可以直接用 Speakr 的「錄音」按鈕現場錄（瀏覽器在 HTTP 下不給麥克風權限）。

## 部署

```bash
git clone https://github.com/yoshikigo/speakr-stack ~/speakr-stack   # 或 gh repo clone yoshikigo/speakr-stack ~/speakr-stack
cd ~/speakr-stack/speakr
./scripts/install.sh
```

`install.sh` 會依序：

1. 檢查 asr-server、Ollama proxy、port 8899、tailnet 主機名稱，以及摘要模型的 `num_ctx`
2. 建立 `~/.config/speakr/speakr.env`（自動填入 `APP_BASE_URL`）
3. 問你管理員密碼，存成 podman secret `speakr-admin-password`
4. 拉 `learnedmachine/speakr:lite`（約 700 MB）
5. 安裝 Quadlet、啟動、等它起來
6. `tailscale serve --bg --https=8899 http://127.0.0.1:8899`

可以重複執行，已存在的設定檔和 secret 不會被覆蓋。8899 在 tailscale 上被占用的話：`TS_PORT=8898 ./scripts/install.sh`。

## 第一次登入後要設定的地方

右上角頭像 → **Account Settings**：

| 項目 | 值 |
|---|---|
| Transcription language | `zh` |
| Output language（摘要、標題用的語言） | `Traditional Chinese (Taiwan)` |
| Timezone | `Asia/Taipei` |

**自訂摘要提示詞**（同一頁的 Custom Prompts / Summary prompt，也可以針對標籤設定），例如：

```
以下是錄音的逐字稿（語音辨識結果，沒有標點，可能有同音錯字）。請用繁體中文整理：

## 摘要
一段話說明這段錄音在講什麼。

## 重點
依主題分組條列。

## 決議
## 待辦事項
- [ ] 誰：做什麼（期限）。逐字稿沒提到負責人或期限就寫「未提及」。

## 需要確認
人名、數字、專有名詞中可能辨識錯誤的地方。

不要編造逐字稿裡沒有的內容。
```

Speakr 上傳時可以在下拉選單選 `whisper-large-v3`（有標點，適合英文多的錄音），預設是 Breeze。

## 模型選擇（重要）

`TEXT_MODEL_NAME` 預設是 `qwen3.8-27b-ud-64k`，因為它的 Modelfile 設了 `num_ctx 65536`。Ollama 的 OpenAI 相容端點**沒辦法從請求指定 context 長度**，模型沒設 `num_ctx` 的話會用很小的預設值，長逐字稿會被默默截斷，摘要只會涵蓋前面一小段。

另一個要注意的是 **Ollama 換模型**：如果 Hermes 用的是另一個 27B（例如 `orcarouter/Qwen3.8-27B-Uncensored`），兩個 27B 塞不進 24 GB，每次 Speakr 和 Hermes 交替使用時 Ollama 都要卸載、重新載入（每次十幾秒）。想避免的話，讓兩邊用同一個模型，例如幫 Hermes 用的模型做一個 64K 版本：

```bash
ollama show orcarouter/Qwen3.8-27B-Uncensored:latest --parameters | grep num_ctx   # 已經 ≥ 32768 就直接用
printf 'FROM orcarouter/Qwen3.8-27B-Uncensored:latest\nPARAMETER num_ctx 65536\n' > /tmp/Modelfile
ollama create qwen3.8-27b-uncensored-64k -f /tmp/Modelfile
sed -i 's/^TEXT_MODEL_NAME=.*/TEXT_MODEL_NAME=qwen3.8-27b-uncensored-64k/' ~/.config/speakr/speakr.env
systemctl --user restart speakr.service
```

## 跨錄音提問（Inquire Mode，選用）

可以問「上個月所有會議裡提到預算的部分」。lite 映像沒有本地 embedding 模型，所以改用 Ollama 的 `qwen3-embedding:4b`。在 `~/.config/speakr/speakr.env`：

```
ENABLE_INQUIRE_MODE=true
EMBEDDING_BASE_URL=http://127.0.0.1:11435/v1
EMBEDDING_API_KEY=ollama
EMBEDDING_MODEL=qwen3-embedding:4b
```

然後 `systemctl --user restart speakr.service`。代價是 VRAM：27B（約 19 GB）＋ ASR（約 2 GB）＋ embedding（約 3 GB）已經頂到 24 GB，Ollama 可能會在幾個模型之間來回卸載。先用一般功能，覺得需要再開。之後若改 embedding 模型，舊錄音要重新建索引（Speakr 啟動時會警告）。

## 從網址匯入（yt-dlp，選用）

在 Speakr 的上傳視窗多一個「從網址匯入」欄位：貼 YouTube（或其他 yt-dlp 支援的網站）網址，按「下載並轉錄」，只下載音訊，下載完自動送進 Speakr 原本的轉錄、摘要流程。進度顯示在畫面右下角，重新整理頁面也會繼續追蹤。

```
上傳視窗的網址欄位 ──POST /url-import/api/import（同源，帶你的 Speakr 登入 cookie）
  └─ tailscale serve 路徑 /url-import → 127.0.0.1:8897  speakr-url（側車）
        ├─ 用你的 cookie 問 Speakr /api/v1/users/me 確認身分
        ├─ yt-dlp：讀資訊 → 只抓音訊 → m4a 96k
        └─ 用同一個 session POST /api/v1/recordings/upload → Speakr 照常轉錄（asr-server）＋摘要（Ollama）
```

安裝（Speakr 已經用 `install.sh` 部署好之後）：

```bash
cd ~/speakr-stack/speakr && ./scripts/install-url-import.sh
```

腳本會 build `localhost/speakr-url`（官方 lite 映像 + 模板補丁 + 獨立 venv 裡的 yt-dlp/deno）、讓 Speakr 改用這個映像、啟動 `speakr-url` 側車，並加上 `tailscale serve --set-path=/url-import`。第一次打開請強制重新整理（Ctrl+Shift+R），避免瀏覽器用到舊的頁面快取。

匯入的錄音會自動帶入：
- 標題：影片標題
- 備註：來源網址、上傳者
- 日期：影片發布日
- 外部連結：原始影片（Speakr 的 external refs）
- 同一支影片 24 小時內重複匯入不會建立第二筆（idempotency key）

限制與安全：
- 預設最長 4 小時（`MAX_DURATION_SECONDS`），播放清單和直播不接受。
- 只接受 http/https，拒絕內網與本機位址（避免被拿來打主機上的其他服務）。
- 必須是已登入的 Speakr 使用者，並要求自訂標頭，其他網站無法借用你的登入發出匯入請求。
- 需要登入才能看的影片（年齡限制、會員）：把瀏覽器匯出的 `cookies.txt` 放在 `~/.config/speakr/yt-cookies.txt`，打開 `speakr-url.container` 裡那兩行註解，`systemctl --user daemon-reload && systemctl --user restart speakr-url`。

維護：

```bash
./scripts/update-ytdlp.sh                       # YouTube 下載開始失敗時：更新 yt-dlp
./scripts/install-url-import.sh                 # Speakr 出新版時：拉新版 lite 並重新套補丁
journalctl --user -u speakr-url -f              # 看匯入流程
```

Speakr 改版如果動到上傳視窗的模板，build 會在補丁那步失敗並說明原因，正在跑的舊映像不受影響。到時把錯誤訊息給我，我調整補丁的錨點。

還原成官方映像：

```bash
cp ~/.config/containers/systemd/speakr.container.bak-url ~/.config/containers/systemd/speakr.container
systemctl --user stop speakr-url && rm ~/.config/containers/systemd/speakr-url.container
systemctl --user daemon-reload && systemctl --user restart speakr
tailscale serve --https=8899 --set-path=/url-import off
```

## 人名查證（選用）

語音辨識最常錯的是人名同音字（例如「沈伯洋」被聽成「沈伯揚」）。分兩層處理：

1. **轉錄時就給提示**（網址匯入自動做）：影片標題＋說明開頭會當成 `initial_prompt` 交給 Breeze，影片說明也存進錄音備註。人名、專有名詞照影片的寫法，從源頭減少錯字。
2. **摘要完成後上網查證**：

```
Speakr 摘要完成 ──webhook（HMAC 簽章）──> speakr-url /hooks/speakr
  ├─ Ollama（不開思考，JSON 輸出）：列出摘要裡的人名與身分
  ├─ SearXNG（127.0.0.1:8090）：搜尋「姓名 身分」
  ├─ Ollama：對照搜尋結果與影片標題/說明 → 正確 / 需修正 / 無法確認
  └─ Speakr API：高信心（≥0.8）的修正直接改摘要，並在最後附「人名查證（自動）」段落與來源連結
```

安裝（需要先裝好網址匯入；SearXNG 要開 JSON 格式，腳本會檢查）：

```bash
cd ~/speakr-stack/speakr && ./scripts/install-namecheck.sh
```

腳本會請你在 Speakr 建一個 API token（Account Settings → API Tokens，Custom 勾 read、write、webhooks），存成 podman secret，然後建立 webhook、讓 Speakr 允許送到本機、重建 speakr-url。最後可以輸入一筆錄音 ID 立刻試跑。

- webhook 自動跑的只改**摘要**（API token 權限只到摘要）；要連逐字稿一起改，用下面的「校正姓名」按鈕。重跑會先移除舊的查證段落。
- 沒把握的不會自動改，會列成 ❓ 並附「可能應為」的建議，讓你自己判斷。
- 跟 Speakr 摘要共用同一個 Ollama 模型，不會造成換模型；查證本身不開思考，通常 1–2 分鐘。
- 手動重跑某筆：`podman exec speakr-url /opt/url-import/bin/python /opt/url-import/namecheck.py <錄音ID>`
- 門檻與上限：drop-in `~/.config/containers/systemd/speakr-url.container.d/namecheck.conf` 加 `Environment=NAMECHECK_MIN_CONFIDENCE=0.9`、`Environment=NAMECHECK_MAX_PEOPLE=20`。

### 逐字稿工具列的「校正姓名」按鈕

逐字稿標題列（↕、Bubble 旁邊）多一個同樣樣式的 **校正姓名** 按鈕（手機版是 👤✓ 圖示）。按下去：

```
瀏覽器 ──(你的登入 session)──> speakr-url /url-import/api/namecheck
  ├─ 同上的查證流程（讀錄音、改摘要都用「你」的身分，沒有編輯權限就會被 Speakr 擋下）
  └─ 回傳修正清單 [[錯, 對], …]
瀏覽器拿到清單 → 讀最新逐字稿 → 逐段替換文字與說話者名稱 → 用 Speakr 自己的「編輯逐字稿」API 存回
```

- 進度和結果顯示在右下角卡片（例如「陳以誠 → 陳以成，逐字稿改了 12 處」），按「重新載入看結果」。
- 不需要 API token 或 webhook，只要裝好網址匯入就有；SearXNG / Ollama 位址沿用 `namecheck.conf` 的設定（沒裝 namecheck 時用預設 127.0.0.1:8090、127.0.0.1:11435）。
- 摘要還沒產生時，改從逐字稿找人名（只修逐字稿）。
- 替換是冪等的：已經是正確寫法的地方不會再被改（「王明 → 王明德」重按不會變成「王明德德」）。
- 逐字稿走 Speakr 原本的存檔路徑，所以 Inquire 的語意索引會跟著重建；改過之後想重新產生摘要，用 Speakr 的「重新摘要」。
- 同一份錄音同時只會跑一個查證（webhook 和按鈕共用鎖），重複按會提示「正在查證中」。

更新方式（已裝網址匯入的話，重建映像即可，Speakr 和 speakr-url 會一起重啟）：

```bash
cd ~/speakr-stack && git pull
cd speakr && YTDLP_REFRESH=keep ./scripts/install-url-import.sh
```

## 繁中翻譯分頁

右側 Summary / Notes / Stats 後面多一個 **繁中翻譯** 分頁；左側逐字稿的「Transcription」旁邊也有一個 **繁中翻譯** 分頁，點了用翻譯取代逐字稿區塊（點「Transcription」切回原文，會記住上次選的）。手機版在摘要標題列的 🌐 圖示，開成全螢幕。每筆錄音都看得到分頁；還在處理中的會顯示狀態，完成後才能翻。按「翻譯成繁體中文」：

```
瀏覽器 ──(你的登入 session)──> speakr-url /url-import/api/translation
  ├─ 摘要：整份 Markdown 交給 Ollama 翻成台灣繁體中文（保留標題/清單/連結，外國人名「譯名（原文）」）
  └─ 逐字稿：每批約 30 段 / 2000 字，要求逐行對應輸出（JSON schema 約束；行數對不上就拆半重試）
結果存在 volume speakr-translations（/data/translations/<錄音ID>.json），Speakr 的摘要與逐字稿完全不動
```

- 逐字稿翻譯是「譯文在上、原文在下」的對照，可以勾掉「顯示原文」只看譯文。
- 每批翻完就存，分頁會每 3 秒更新進度；可以先離開，回來再看。長錄音（1 小時）大約十幾分鐘。
- 摘要改過之後分頁會提示「翻譯過期」，按「重新翻譯」。逐字稿沒變的話會沿用舊翻譯，只重翻摘要。
- 同一時間只跑一個翻譯工作（排隊），避免跟摘要、人名查證搶 Ollama。
- 權限沿用 Speakr：看得到錄音的人才看得到翻譯。
- 換翻譯模型：drop-in 加 `Environment=TRANSLATE_MODEL=...`（預設跟人名查證同一個模型）。
- 清掉所有翻譯：`systemctl --user stop speakr-url && podman volume rm speakr-translations && systemctl --user start speakr-url`。

## 一鍵匯入 Obsidian

錄音標題列分享鈕旁邊的 💎 **匯入 Obsidian**（手機版在標題列下方那排圖示）。按一下就把這筆錄音寫成一篇 Markdown：

```
vault/Speakr/2026-09-23 Teaching in the Age of AI….md
  ├─ frontmatter：title、date、speakr_id、speakr_url、source（原始影片）、duration、participants、tags
  ├─ ## 摘要（原文；摘要裡的標題自動降兩級）
  ├─ ## 摘要（繁中翻譯）        ← 有翻譯才有
  ├─ ## 筆記
  └─ ## 逐字稿（附繁中翻譯）    ← [時間] 說話者 原文，下一行 > 譯文；同一人連續發言只寫時間
```

寫進 arch 上的明文 vault 後，arch 的 Obsidian 會看到新檔，LiveSync 幾秒內同步到手機、Mac。

安裝（要先有 obsidian-server，且已經用新版 install-url-import.sh 重建過映像）：

```bash
cd ~/speakr-stack/speakr && ./scripts/install-obsidian-export.sh
```

- 只把 vault 的 `Speakr/` 資料夾（可改 `OBSIDIAN_FOLDER=…`）掛進 speakr-url，其他筆記碰不到。vault 位置自動從 `~/.config/obsidian-server/env` 讀，或用 `VAULT_DIR=…` 指定。
- 同一筆再按一次：會問要不要用最新內容更新；如果這篇在 Obsidian 裡被改過，會特別警告「覆蓋會蓋掉你的修改」。
- 說話者先改好名字（Speakr 的辨識說話者 / 重新轉錄指定人數）再匯入，逐字稿才會是人名而不是 SPEAKER_xx。
- 外接硬碟沒掛上時 speakr-url 會起不來（跟 Hermes 的 vault 掛載一樣）；掛好後 `systemctl --user restart speakr-url`。

## 模型選單一覽

| 選單 | 模型 | 適合 |
|---|---|---|
| A（預設） | Breeze-ASR-25 + sherpa 分辨，語言固定中文 | 台灣華語、中英夾雜 |
| A2 | Whisper large-v3 + sherpa 分辨，語言自動偵測 | 純英文或其他外語（標點、斷句較好） |
| A3 | Breeze-ASR-26 + sherpa 分辨，輸出中文字 | 台語、台華夾雜（asr-server 跑過 `add-breeze26.sh` 後，再跑 `use-asr-diarize.sh` 才會出現） |
| B | WhisperX（轉錄同 A，只有對齊/分辨不同） | A/B 比較用 |

## 說話者分辨 A/B：asr-server（A）vs WhisperX（B）

```
Speakr ──/asr?model=…──> asr-server（127.0.0.1:8098）
                           ├─ breeze-asr-25 / whisper-large-v3 → A：GPU 轉錄 ＋ CPU sherpa-onnx 分辨（平行）
                           └─ whisperx                         → B：轉送 WhisperX（轉錄同樣是 Breeze）
```

先升級 asr-server（`asr-server.tar.gz` 的 `./scripts/upgrade.sh`），再讓 Speakr 改用它：

```bash
cd ~/speakr-stack/speakr && ./scripts/use-asr-diarize.sh
```

之後在 Speakr **上傳時**或錄音頁 ↻ ▾ **Reprocess Transcription** 的模型選單選：

| 選項 | 內容 |
|---|---|
| A：Breeze＋sherpa 分辨（預設） | 不選就是這個 |
| B：WhisperX 分辨 | 有裝 WhisperX 才會出現 |
| A2：Whisper large-v3＋sherpa 分辨 | 英文多、要標點時 |

**在 Speakr 裡比較同一段錄音**：同一個檔案上傳兩次（或網址匯入後再手動上傳一次原檔），一筆選 A、一筆選 B，並排看逐字稿和 Stats 分頁。重新轉錄會覆蓋原本的結果，所以要並排看就用兩筆。

**命令列比較**（更客觀，會算一致率、列出兩邊判斷不同的時段）：見 asr-server 的 `./scripts/ab-diarize.sh 錄音.m4a [人數]`。

腳本最後會問要不要把 WhisperX 改成 CPU：B 只拿來比較的話建議改，讓出約 2 GB VRAM，避免 Ollama 摘要被擠到 CPU。

還原：`cp ~/.config/speakr/speakr.env.bak-diarize ~/.config/speakr/speakr.env && systemctl --user restart speakr`

> Speakr 的 Stats「WORDS」是用空白斷詞，中文會嚴重少算（整段只算一兩個字），不用參考；看 TIME 和 TURNS 就好。

## 說話者分辨（WhisperX，選用）

```
Speakr ──/asr──> whisperx（127.0.0.1:9010，Quadlet，GPU）
                   ├─ 轉錄 → asr-server /v1/audio/transcriptions（Breeze-ASR-25，照舊）
                   ├─ Qwen3 forced aligner：字級時間（支援中英夾雜、繁體字）
                   ├─ pyannote community-1：誰在什麼時候說話
                   └─ 聲紋 embedding：之後的錄音自動認出同一個人
```

使用 Speakr 作者維護的 [whisperx-asr-service](https://github.com/murtaza-nasir/whisperx-asr-service) 0.4.2 的 `ASR_BACKEND=external` 模式：轉錄仍交給 Breeze，WhisperX 只負責對齊、分辨和聲紋，所以台灣華語的辨識品質不變。

**事前準備（Hugging Face，免費）**：登入後到下面三頁各按一次「Agree and access repository」，再到 Settings → Tokens 建一個 Read 權限的 token：

- https://huggingface.co/pyannote/speaker-diarization-community-1
- https://huggingface.co/pyannote/segmentation-3.0
- https://huggingface.co/pyannote/speaker-diarization-3.1

```bash
cd ~/speakr-stack/speakr && ./scripts/install-whisperx.sh
```

腳本會：存 token 成 podman secret `hf-token` 並確認三個模型都有權限 → 把 asr-server 的上傳上限放寬到 1 GB（WhisperX 會把整段錄音轉成 WAV 一次送）→ 拉 WhisperX 映像（PyTorch + CUDA，好幾 GB，網路慢會很久，中斷後重跑會接著拉）→ 安裝 Quadlet → 用兩種機器聲音做冒煙測試（第一次會再下載約 1.5 GB 模型）→ 把 Speakr 切到 `asr_endpoint`。

切換後：

- 新上傳的錄音逐字稿會標 `SPEAKER_00`、`SPEAKER_01`…，在錄音頁把標籤改成真名，摘要裡的待辦事項就會對上人。
- 每個人需要約 15 秒以上的發言才會建立聲紋；之後的錄音會自動標出認得的人。
- 每段錄音可在上傳選項裡指定最少/最多人數，人數已知時分辨會更準。
- 上傳視窗不再有模型下拉選單（轉錄固定走 Breeze）；網址匯入的模型選單也已拿掉，重跑一次 `YTDLP_REFRESH=keep ./scripts/install-url-import.sh` 更新。
- 已經轉好的舊錄音不會自動重跑，需要的話在 Speakr 裡對那筆按重新轉錄。

調整（`~/.config/whisperx/whisperx.env`，改完 `systemctl --user restart whisperx`）：

| 狀況 | 設定 |
|---|---|
| 兩個聲音很像，被併成同一人 | `DIARIZE_CLUSTERING_THRESHOLD=0.5`（預設 0.6，越低越容易拆開） |
| 一個人被拆成好幾個 | 調高到 0.7 |
| VRAM 不夠、Ollama 摘要變很慢 | `DEVICE=cpu`：WhisperX 的對齊和分辨改用 CPU（轉錄仍在 GPU），1 小時錄音多幾分鐘 |

**VRAM**：Ollama 27B（約 19 GB）＋ Breeze（約 2 GB）＋ WhisperX（CUDA context＋Qwen 對齊約 1.3 GB＋pyannote）大約 23 GB 多，很接近 24 GB。pyannote 閒置 5 分鐘會卸載，但 Qwen 對齊模型會留在 GPU 上。如果 `nvidia-smi` 看到快滿、或 Ollama 摘要明顯變慢（`ollama ps` 顯示部分在 CPU），就把 WhisperX 改成 `DEVICE=cpu`。

**還原**（Speakr 改回直接用 asr-server、停掉 WhisperX）：

```bash
./scripts/rollback-whisperx.sh
```

## 日常操作

```bash
journalctl --user -u speakr -f                   # Speakr log
journalctl --user -u asr-server -f               # 看轉錄進度（每 10 分鐘一段）
systemctl --user restart speakr.service          # 改完 speakr.env 後
podman pull docker.io/learnedmachine/speakr:lite && systemctl --user restart speakr.service   # 更新
```

**備份**（停機備份最安全，SQLite 在 `speakr-instance`）：

```bash
systemctl --user stop speakr.service
podman volume export speakr-instance > ~/backup/speakr-instance-$(date +%F).tar
podman volume export speakr-uploads  > ~/backup/speakr-uploads-$(date +%F).tar
systemctl --user start speakr.service
```

## 移除

```bash
tailscale serve --https=8899 off            # 或 sudo
systemctl --user stop speakr.service
rm ~/.config/containers/systemd/speakr.container && systemctl --user daemon-reload
# 連資料一起刪（不可復原）：
# podman volume rm speakr-uploads speakr-instance; podman secret rm speakr-admin-password; rm -r ~/.config/speakr
```

## 疑難排解

| 症狀 | 檢查 |
|---|---|
| 轉錄失敗 | `journalctl --user -u asr-server -n 30`；`curl -sS localhost:8098/health` |
| 摘要一直轉圈或逾時 | `journalctl --user -u speakr -n 50`；`ollama ps` 看模型是否在載入；`LLM_REQUEST_TIMEOUT` |
| 摘要只涵蓋前面一小段 | 模型 `num_ctx` 太小，見「模型選擇」 |
| 登入後馬上被登出 | 一定要用 `https://...ts.net:8899` 開（`SESSION_COOKIE_SECURE=true`，用 http 開 cookie 不會存） |
| 手機錄音按鈕沒反應 | 同上，要 HTTPS；並允許瀏覽器使用麥克風 |
| `tailscale serve` 權限錯誤 | `sudo tailscale set --operator=$USER` 後重跑，或直接用 sudo |

## 已驗證 / 未驗證

- ✅ 對照 Speakr 原始碼（v0.10.13-alpha）確認：`openai_whisper` 連接器用 OpenAI SDK 送 `model/file/language/prompt`，asr-server 都支援；切段是 mp3，10 分鐘約 10 MB，在 asr-server 的 50 MB 上限內；摘要的 `<think>` 會被濾掉；lite 映像用 API embeddings 時仍可做語意搜尋。
- ⚠️ 沒實際拉過映像跑起來（我的環境連不到 Docker Hub）。Speakr 是 alpha 版，升級前先備份。
- ✅ 網址匯入：模板補丁對 Speakr 原始碼目前的模板測過（插入成功、Jinja 解析正常、重複執行會略過）；側車服務用假的 Speakr 和假的 yt-dlp 跑過完整流程（登入驗證、CSRF 標頭、拒絕內網/非 http 網址、下載進度、上傳欄位、錯誤訊息、他人無法查看工作、暫存檔清除）。
- ⚠️ 未實測：真的 yt-dlp 下載 YouTube、官方 `lite` 映像裡的模板是否和原始碼一致（不一致時 build 會直接失敗並說明）、`tailscale serve --set-path` 是否去掉路徑前綴（兩種情況側車都能處理）。
- ✅ WhisperX：對照 whisperx-asr-service 0.4.2 原始碼確認 external 模式的請求格式（整檔 16 kHz WAV、`verbose_json`、帶 `language`）與 asr-server 相容；Speakr `asr_endpoint` 連接器不帶模型名（所以必須拿掉 `TRANSCRIPTION_MODEL`，否則 WhisperX 回 400）；改設定檔的輔助函式測過。
- ⚠️ WhisperX 未實測：映像沒實際拉過（`0.4.2` 標籤若不存在，install 會在拉映像那步失敗，改用 `:latest` 前要先確認含 external backend——腳本會自動檢查）、VRAM 實際用量、真人錄音的分辨品質。
- ✅ 人名查證：用假的 Speakr / SearXNG / Ollama 跑過完整流程（列人名 → 搜尋 → 判斷 → 修正摘要 → 附查證段落、重跑不重複、webhook 簽章正確/錯誤兩種情況）；Speakr webhook 簽章格式、內網白名單、摘要 PUT API 都對照原始碼確認。
- ✅ 「校正姓名」按鈕：模板補丁對 Speakr v0.10.13 原始碼套過（桌面工具列、手機標題列，重跑不重複）；後端用假 Speakr / Ollama / SearXNG 跑過（session 驗證、同錄音重複按回 409、缺 CSRF 標頭回 403、改摘要）；前端逐字稿替換用模擬 fetch 測過（JSON 逐字稿的文字與說話者都換到、時間/ID 欄位不動、送 update_transcript）。
- ✅ 繁中翻譯：模板補丁對 Speakr v0.10.13 套過（桌面分頁＋面板、手機圖示，重跑不重複）；後端用假 Speakr / Ollama 跑過（權限 401/403、重複按 409、摘要去掉 ``` 包裝、模型少回一行時拆半重試且逐行對齊正確、存檔）；面板與手機全螢幕檢視在 headless Chromium 繪製過、無 JS 錯誤。
- ✅ 匯入 Obsidian：用假 Speakr 跑過（frontmatter、標題降級、譯文逐段對齊、同一人連續發言合併、重複匯入 409、Obsidian 裡改過會標 edited、overwrite 覆蓋）；模板補丁對 v0.10.14 套過。
- ⚠️ 匯入 Obsidian 未在真的 vault / LiveSync 上測過（exFAT 上的檔名、LiveSync 偵測外部新增檔案）。
- ⚠️ 翻譯分頁未在真的 Speakr 頁面裡切換過（Vue 重繪時面板是否保持）；27B 長逐字稿的實際翻譯速度未測。
- ⚠️ 按鈕未在真的瀏覽器裡點過；人名查證未實測：真實 SearXNG 搜尋結果的品質、27B 在 `think:false` 下的判斷準確度。

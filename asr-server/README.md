# asr-server — LINE 語音訊息 → Breeze-ASR-25 / Whisper large-v3（3090）→ Hermes

```
LINE 語音訊息 (m4a)
  └─> line.example.com/line/webhook ─(arch-hermes tunnel)─> Hermes LINE adapter :8646
        └─ audio → MessageType.VOICE → Hermes STT（stt.provider: openai）
              └─ POST http://127.0.0.1:8098/v1/audio/transcriptions
                    └─ asr-server（Quadlet，3090，faster-whisper）
                          ├─ breeze-asr-25      ← 預設，台灣華語 / 中英夾雜
                          └─ whisper-large-v3   ← 通用、多語
        └─ 逐字稿當成你打的文字交給 agent（qwen3.8-27b）回覆
```

不用另外寫 LINE bot：目前的 Hermes LINE adapter 已經把 LINE 的 audio 訊息歸類成 VOICE、走 STT 流程，只要把 STT 指到這個服務。webhook、tunnel、allowlist 全部不動。

## 0. 前置：讓 rootless Podman 看得到 3090（CDI）

Ollama 跑在 host 上，所以容器 GPU 可能還沒設過：

```bash
sudo pacman -S --needed nvidia-container-toolkit
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml   # Core X 要插著
nvidia-ctk cdi list                                           # 應有 nvidia.com/gpu=all
podman run --rm --device nvidia.com/gpu=all docker.io/library/python:3.12-slim nvidia-smi -L
```

nvidia 驅動升級後要重跑一次 `cdi generate`，不然容器會起不來（驅動函式庫路徑變了）。

## 1. Build

```bash
tar xzf asr-server.tar.gz && cd asr-server
podman build -t localhost/asr-server:latest .
ss -ltnp | grep 8098 || echo "8098 free"
```

## 2. 下載 / 轉換模型（一次性，進 volume `asr-models`）

```bash
./scripts/fetch-models.sh
```

- large-v3：直接抓 Systran 的 CTranslate2 版。
- Breeze-ASR-25：從**官方** `MediaTek-Research/Breeze-ASR-25` 下載後在一次性容器裡自己轉（CPU torch，約 5–10 分鐘），不依賴第三方轉好的權重。想釘版本：`BREEZE_REVISION=<commit sha> ./scripts/fetch-models.sh`。
- 嫌慢的話，HF 上也有別人轉好的 `shdennlin/breeze-asr-25-ct2`（int8_float16 子目錄）可以直接放進 `/models/breeze-asr-25`，但那是第三方檔案，自己斟酌。

## 3. 先手動跑起來測（不經 systemd）

```bash
podman run --rm --device nvidia.com/gpu=all -p 127.0.0.1:8098:8098 \
  -v asr-models:/models:ro localhost/asr-server:latest
```

另一個終端，拿一段語音（LINE 存下來的 m4a 就行）兩個模型各打一次比較：

```bash
curl -s localhost:8098/health
for m in breeze-asr-25 whisper-large-v3; do
  curl -s -F file=@test.m4a -F model=$m -F response_format=verbose_json \
    http://127.0.0.1:8098/v1/audio/transcriptions | python -m json.tool
done
nvidia-smi    # 看 Ollama + ASR 加起來的 VRAM
```

第一次請求會載入模型（約 5–15 秒），之後幾秒內回來。

## 4. 裝成 Quadlet

```bash
cp quadlet/asr-server.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start asr-server.service
journalctl --user -u asr-server -f
```

## 5. 接上 Hermes

```bash
podman unshare cp -p ~/.hermes/config.yaml ~/.hermes/config.yaml.bak-asr
podman unshare vim ~/.hermes/config.yaml      # 把 hermes/stt-config.yaml 那段貼進去，取代原本的 stt:
systemctl --user restart hermes.service
```

用 LINE 傳一段語音給 bot，然後看兩邊 log：

```bash
journalctl --user -u asr-server -n 20           # 應出現「breeze-asr-25：音訊 Xs，耗時 Ys」
podman logs --since 5m hermes 2>&1 | grep -iE "transcri|stt"   # 應出現 Transcribed ... via openai
```

**如果 asr-server 完全沒收到請求**：多半是 Hermes 映像太舊，LINE adapter 還沒把 audio 當 VOICE。`podman pull docker.io/nousresearch/hermes-agent:latest && systemctl --user restart hermes.service` 再試。

## 說話者分辨（/asr）與 A/B 比較

`/asr` 是 Speakr `asr_endpoint` 連接器用的介面：

- **A（預設）**：Breeze 在 GPU 上轉錄（開字級時間戳），同時 sherpa-onnx 在 CPU 上做說話者分辨（pyannote segmentation-3.0 ＋ 3D-Speaker CAM++ 中英聲紋，約 35 MB，建置時下載並驗 sha256）。兩者平行跑，再把每個字分給說話者、一換人就切段。
- **B**：`/asr?model=whisperx` 會整包轉送給 WhisperX（`ASR_WHISPERX_URL`），方便同一個介面做 A/B。
- `/v1/audio/transcriptions`（LINE、Open WebUI、WhisperX external 模式在用）完全不變。

升級已部署的 asr-server：

```bash
cd ~/speakr-stack && git pull
cd asr-server && ./scripts/upgrade.sh
```

注意：Quadlet 改成 `Network=host` 並只綁 `127.0.0.1:8098`（原本是 PublishPort），這樣它才連得到 WhisperX 的 127.0.0.1:9010。對其他服務沒有影響（它們本來就打 127.0.0.1:8098）。

**命令列 A/B**（同一個檔案兩邊各跑一次，輸出報告和兩份逐字稿）：

```bash
./scripts/ab-diarize.sh 錄音.m4a        # 不知道人數
./scripts/ab-diarize.sh 錄音.m4a 4      # 已知 4 個人
```

報告內容：兩邊分出幾人、換人次數、處理時間、「誰在說話」一致率（自動對應說話者），以及兩邊判斷不同的最長幾段時間——拿那幾段去聽，就知道哪邊比較準。

調整 A（`asr-server.container`，改完 `systemctl --user daemon-reload && systemctl --user restart asr-server`）：

| 狀況 | 設定 |
|---|---|
| 不同人被併成同一人 | `ASR_DIAR_THRESHOLD=0.4`（預設 0.5，越低拆越細） |
| 同一人被拆成好幾個 | `ASR_DIAR_THRESHOLD=0.6` |
| 分辨太慢 | `ASR_DIAR_THREADS` 調高（預設 8，155H 有 22 執行緒） |
| 已知人數 | 在 Speakr 上傳時填最少/最多人數（相同即固定人數），比調門檻更準 |

## A3：Breeze-ASR-26（台語）

MediaTek Research 的台語語音辨識（Whisper large-v2 微調、Apache 2.0），台語或台華夾雜的錄音轉成中文字（再經 OpenCC 轉繁體台灣用語）。

```bash
cd ~/speakr-stack/asr-server && ./scripts/add-breeze26.sh     # 下載＋轉檔＋加進 ASR_MODELS＋重啟
cd ~/speakr-stack/speakr && ./scripts/use-asr-diarize.sh           # Speakr 選單多出「A3：Breeze-ASR-26 台語」
```

- LINE / Open WebUI 也能用：`model=breeze-asr-26`。
- `ASR_MAX_LOADED=1`：同時只常駐一個模型，切換時自動卸載，不會多吃 VRAM（但切換那次要多 5–10 秒載入）。
- 注意：**Breeze Guard 26** 是同系列的文字內容安全分類模型，不是語音模型，不能拿來轉錄。

## 換模型 / 調整

| 想做的事 | 改哪裡 |
|---|---|
| Hermes 改用 large-v3 | `config.yaml` 的 `stt.openai.model: whisper-large-v3`，重啟 hermes |
| 兩個模型常駐（不互相卸載） | Quadlet `ASR_MAX_LOADED=2`（多吃約 2 GB VRAM） |
| 閒置多久釋放 VRAM | `ASR_IDLE_UNLOAD`（秒，預設 600；0 = 不卸載） |
| 品質優先 | `ASR_COMPUTE_TYPE=float16`（VRAM 約翻倍） |
| 跑大型 Ollama 工作前手動清 VRAM | `curl -X POST localhost:8098/admin/unload` |
| 各模型沒指定語言時的預設 | `ASR_MODEL_LANGUAGES`（預設 Breeze 25/26 = zh，large-v3 = auto 自動偵測；呼叫端有給語言就照給的） |
| 不要繁簡轉換 | `ASR_OPENCC=`（預設 s2twp，主要是修正 large-v3 偶爾吐簡體） |
| 加 API key | `podman secret create asr-api-key -`，打開 Quadlet 的 `Secret=` 行，Hermes 填 `stt.openai.api_key` |

## LINE 免費額度

`echo_transcripts: true` 會先把逐字稿回給你，這一則會用掉 LINE 的免費 reply token，所以 agent 的正式回答就得改走計費的 Push API。測完覺得辨識可靠，就改成 `false`，讓回答保留 reply token（或 Hermes 的「Get answer」按鈕流程）。

## 順便可以用的地方

- **Open WebUI**：設定 → 音訊 → STT 引擎選 OpenAI，API Base URL `http://127.0.0.1:8098/v1`、key 隨便填、模型 `breeze-asr-25`。它送 `whisper-1` 也會對應到預設模型。
- **Prometheus / Grafana**：`/metrics` 有 `asr_requests_total`、`asr_audio_seconds_total`、`asr_processing_seconds`、`asr_model_loaded`。scrape target `127.0.0.1:8098`（Prometheus 若不是 `Network=host`，改用 `host.containers.internal:8098`，並把 Quadlet 的 PublishPort 調整成它連得到的位址）。

## VRAM 預算

**CUDA 出錯會自動重啟**：轉錄時遇到 CUDA 錯誤（最常見是 VRAM 不夠導致 OOM，之後 GPU 狀態會壞掉、每個請求都報 `invalid device ordinal`），asr-server 會回 503 並在 1 秒後結束行程，由 systemd 重啟，順便釋放 VRAM。log 會出現 `CUDA 錯誤…重啟 asr-server`；要是頻繁出現，就是 VRAM 真的不夠用（見下）。

Ollama `qwen3.8-27b-ud-64k` 約 18.9 GB；一個 large 級 Whisper 用 `int8_float16` 約 2 GB 上下，合計還在 24 GB 內。要注意的是 Ollama 是在**載入模型當下**估算可用 VRAM：如果 ASR 正好常駐、Ollama 又要載入另一個模型（例如 `qwen3-embedding:4b`），可能會被擠到部分跑 CPU。預設 10 分鐘閒置卸載就是為了減少這種情況。

## 已驗證 / 未驗證

- ✅ API 層（OpenAI SDK `audio.transcriptions.create`、json/text/verbose_json/srt/vtt、別名 `whisper-1`、API key、錯誤格式、LRU 卸載、/metrics）用替身模型測過。
- ✅ Hermes 端的設定方式是對照 hermes-agent 目前原始碼確認的：`stt.openai.base_url` 指本機位址時不需要 key；LINE adapter 把 audio 映射為 VOICE。
- ⚠️ 沒在真的 3090 上跑過 CUDA 推論與模型轉檔（我的環境連不到 Hugging Face）。第 3 步手動測試就是為了先抓這類問題；若出現 `libcudnn` 找不到之類的錯誤，在 Containerfile 補一行 `"nvidia-cudnn-cu12==9.*"` 並把它的 lib 目錄加進 `LD_LIBRARY_PATH`。

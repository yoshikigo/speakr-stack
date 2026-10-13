# Speakr 錄音整理 stack（rootless Podman Quadlet + 本機 GPU 語音辨識）

自架的錄音／會議筆記系統：本機 GPU 語音轉文字、說話者分辨、Ollama 摘要，全部跑在自己的機器上。

| 目錄 | 內容 |
|---|---|
| [`asr-server/`](asr-server/) | OpenAI 相容的語音轉文字服務（faster-whisper）：Breeze-ASR-25（台灣華語）、Breeze-ASR-26（台語）、Whisper large-v3；sherpa-onnx 說話者分辨；給 Speakr、LINE bot（Hermes）、Open WebUI 共用 |
| [`speakr/`](speakr/) | [Speakr](https://github.com/murtaza-nasir/speakr) 部署包與擴充：網址匯入（yt-dlp）、人名上網查證、校正姓名、繁中翻譯分頁、一鍵匯入 Obsidian、WhisperX A/B |

```
瀏覽器 ─ tailscale serve ─> Speakr (127.0.0.1:8899) ──/asr──> asr-server (127.0.0.1:8098, RTX 3090)
                              │                                   └─(選用 B)─> WhisperX (127.0.0.1:9010)
                              ├─ Ollama (127.0.0.1:11435) 摘要 / 翻譯 / 人名查證
                              └─ speakr-url 側車 (127.0.0.1:8897)：yt-dlp、查證、翻譯、Obsidian 匯出
```

## 環境需求

- x86_64、NVIDIA GPU + 驅動 + nvidia-container-toolkit（CDI）
- Podman ≥ 4.4（Quadlet），systemd user session（`loginctl enable-linger`）
- 主機上的 Ollama；選用：Tailscale、SearXNG、obsidian-server
- 已在 Arch Linux 使用；Fedora 40+、RHEL 9.4+、Ubuntu 24.04、Debian 13 理論上可用（未測）

安裝步驟見各目錄的 README：先 `asr-server/`，再 `speakr/`。

```bash
git clone https://github.com/yoshikigo/speakr-stack ~/speakr-stack
cd ~/speakr-stack && git pull          # 之後更新
```

### 從 tar.gz 舊目錄搬過來

以前把 `asr-server.tar.gz` 解到 `~/asr/asr-server`、`speakr.tar.gz` 解到 `~/speakr` 的話：

```bash
cd ~/speakr-stack && ./migrate-from-tarball.sh
```

會檢查已安裝的 Quadlet / systemd unit 有沒有指到舊目錄（有就改成 `~/speakr-stack/...`，先備份）、把只有舊目錄才有的檔案複製進 repo、再把舊目錄移到 `~/.cache/speakr-stack-old/`（不刪）。不會停服務。

## 授權

本 repo 的程式與腳本：Apache-2.0（見 [LICENSE](LICENSE)）。

第三方元件各自的授權不變：Speakr 本體為 AGPL-3.0（本 repo 不含 Speakr 原始碼，但 `speakr/url-import/` 會在建置映像時修補 Speakr 的模板，產生的映像屬於 Speakr 的衍生作品，若對外提供服務需遵守 AGPL）；WhisperX 為 BSD-2；模型各有授權（Breeze-ASR 系列 Apache-2.0）。

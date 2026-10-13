#!/usr/bin/env bash
# YouTube 改版導致下載失敗時：重建映像拿最新 yt-dlp（只重跑 yt-dlp 那一層），重啟兩個容器。
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
podman build -t localhost/speakr-url:latest --build-arg "YTDLP_REFRESH=$(date +%s)" "$HERE/url-import"
systemctl --user restart speakr-url.service speakr.service
sleep 3
curl -fsS http://127.0.0.1:8897/api/health; echo

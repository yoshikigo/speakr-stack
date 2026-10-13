#!/usr/bin/env python3
"""比較兩份 /asr 結果（A：asr-server + sherpa-onnx，B：WhisperX），輸出 Markdown 報告。

只用標準函式庫。沒有標準答案，所以報告重點是：
  * 各自分出幾個人、切了幾段、每個人講多久
  * 兩邊「誰在說話」的一致率（自動找最佳的說話者對應）
  * 兩邊意見不同的時段（最長的前幾段）→ 拿去實際聽，判斷誰對
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict

FRAME = 0.1  # 秒


def load(path: str) -> list[dict]:
    d = json.load(open(path, encoding="utf-8"))
    if "segments" not in d:
        raise SystemExit(f"{path} 不是 /asr 的結果：{str(d)[:200]}")
    segs = [s for s in d["segments"] if s.get("start") is not None and s.get("end") is not None]
    last = None
    for s in segs:  # 少數片段沒有 speaker（WhisperX 偶爾會）→ 沿用前一個
        s["speaker"] = s.get("speaker") or last or "UNKNOWN"
        last = s["speaker"]
    return segs


def frames(segs: list[dict], n: int) -> list:
    lab = [None] * n
    for s in segs:
        for i in range(int(s["start"] / FRAME), min(n, int(s["end"] / FRAME) + 1)):
            lab[i] = s["speaker"]
    return lab


def stats(segs: list[dict]) -> dict:
    talk = defaultdict(float)
    turns = 0
    prev = None
    for s in segs:
        talk[s["speaker"]] += s["end"] - s["start"]
        if s["speaker"] != prev:
            turns += 1
            prev = s["speaker"]
    return {"speakers": len(talk), "segments": len(segs), "turns": turns,
            "talk": dict(sorted(talk.items(), key=lambda kv: -kv[1]))}


def best_mapping(a: list, b: list) -> dict:
    """A 的說話者 → B 的說話者，一對一、依重疊時間由大到小貪婪配對。"""
    overlap = Counter((x, y) for x, y in zip(a, b) if x is not None and y is not None)
    used_a, used_b, mapping = set(), set(), {}
    for (x, y), _ in overlap.most_common():
        if x not in used_a and y not in used_b:
            mapping[x] = y
            used_a.add(x)
            used_b.add(y)
    return mapping


def text_at(segs: list[dict], t0: float, t1: float, limit: int = 60) -> str:
    txt = "".join(s["text"] for s in segs if s["end"] > t0 and s["start"] < t1).strip()
    return (txt[:limit] + "…") if len(txt) > limit else txt


def hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--time-a", type=float)
    ap.add_argument("--time-b", type=float)
    ap.add_argument("--name-a", default="A：asr-server + sherpa-onnx")
    ap.add_argument("--name-b", default="B：WhisperX")
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    A, B = load(args.a), load(args.b)
    end = max([s["end"] for s in A + B] or [0])
    n = int(end / FRAME) + 2
    fa, fb = frames(A, n), frames(B, n)
    m = best_mapping(fa, fb)
    both = [(x, y) for x, y in zip(fa, fb) if x is not None and y is not None]
    agree = sum(1 for x, y in both if m.get(x) == y)
    sa, sb = stats(A), stats(B)

    out = [f"# A/B 說話者分辨比較\n", f"錄音長度約 {hms(end)}。\n",
           "| | " + args.name_a + " | " + args.name_b + " |", "|---|---|---|",
           f"| 處理時間 | {args.time_a or '—'} 秒 | {args.time_b or '—'} 秒 |" if args.time_a or args.time_b else "",
           f"| 分出幾人 | {sa['speakers']} | {sb['speakers']} |",
           f"| 片段數 | {sa['segments']} | {sb['segments']} |",
           f"| 換人次數 | {sa['turns']} | {sb['turns']} |"]
    out = [x for x in out if x != ""]
    out.append("")
    out.append(f"**「誰在說話」一致率：{100 * agree / max(1, len(both)):.1f}%**"
               f"（兩邊都判定有人說話的 {len(both) * FRAME / 60:.1f} 分鐘中；說話者已自動對應）\n")
    out.append("說話者對應（A → B）：" + "、".join(f"{x} → {y}" for x, y in sorted(m.items())) + "\n")
    for title, st in (("A 各說話者時間", sa), ("B 各說話者時間", sb)):
        out.append(f"**{title}**：" + "、".join(f"{k} {v / 60:.1f} 分" for k, v in st["talk"].items()) + "\n")

    # 意見不同的時段
    spans, cur = [], None
    for i, (x, y) in enumerate(zip(fa, fb)):
        bad = x is not None and y is not None and m.get(x) != y
        if bad and cur and cur[2] == (x, y) and i - cur[1] <= 3:
            cur[1] = i
        elif bad:
            if cur:
                spans.append(cur)
            cur = [i, i, (x, y)]
    if cur:
        spans.append(cur)
    spans = sorted(spans, key=lambda s: s[1] - s[0], reverse=True)[: args.top]
    if spans:
        out.append(f"## 兩邊判斷不同的時段（最長的 {len(spans)} 段，拿去聽聽看誰對）\n")
        out.append("| 時間 | 長度 | A 說是 | B 說是 | 內容 |")
        out.append("|---|---|---|---|---|")
        for s0, s1, (x, y) in sorted(spans):
            t0, t1 = s0 * FRAME, (s1 + 1) * FRAME
            out.append(f"| {hms(t0)}–{hms(t1)} | {t1 - t0:.1f}s | {x} | {y}"
                       f"（A 的 {next((k for k, v in m.items() if v == y), '無對應')}） | {text_at(A, t0, t1)} |")
    else:
        out.append("兩邊判斷完全一致。")
    print("\n".join(out))


if __name__ == "__main__":
    main()

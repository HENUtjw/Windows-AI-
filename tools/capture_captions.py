#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抓取 Windows 实时字幕的**真实**输出，用于校准断句逻辑。

为什么需要
----------
实时字幕的文本格式只能实测，猜不出来：换行符用 ``\\r`` 还是 ``\\n``、更新是
「追加」还是「整句改写」、一句话分几次推上来——这些都直接决定
``captions.stable_ms`` 该设多少、断句规则该怎么写。

本工具把每一次变化连同精确时间戳记录下来，供事后分析和回归测试。

运行::

    # 自己播视频，工具只负责记录
    python tools/capture_captions.py --seconds 60

    # 没视频也能测：用 Windows 自带语音合成念一段英文
    python tools/capture_captions.py --speak --seconds 45

    # 念指定文件里的文本
    python tools/capture_captions.py --speak --text-file script.txt
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from src.caption_reader import CaptionReader  # noqa: E402

DEFAULT_SPEECH = (
    "Economics is not just about money. "
    "It studies how people use limited resources. "
    "Every choice you make has a cost, and economists call that the opportunity cost. "
    "When you watch this video you could be doing something else instead. "
    "That trade-off is the heart of the subject we are going to study together. "
    "Let us begin with a simple question about scarcity."
)


def speak_async(text: str, rate: int = -1) -> subprocess.Popen:
    """用 Windows 自带 SAPI 念英文（不引入任何依赖）。

    文本走 base64，避免中文/引号在命令行里被转义搞坏。
    """
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {rate}; $s.Volume = 100; "
        "$s.Speak([Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{payload}')))"
    )
    return subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def capture(reader: CaptionReader, seconds: float) -> list[dict]:
    samples: list[dict] = []
    last = None
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        state = reader.read()
        if state.text != last:
            last = state.text
            elapsed = time.monotonic() - started
            samples.append({"t": round(elapsed, 3), "status": state.status, "raw": state.text})
            if state.text:
                print(f"  [{elapsed:6.2f}s] {state.text!r}")
        time.sleep(0.1)
    return samples


def analyse(samples: list[dict]) -> None:
    """把真实数据的关键特征打出来，供调参参考。"""
    texts = [s["raw"] for s in samples if s["raw"]]
    print("\n" + "=" * 70)
    print("真实字幕特征分析")
    print("=" * 70)
    print(f"  采样点总数        : {len(samples)}")
    print(f"  非空文本数        : {len(texts)}")
    print(f"  去重后不同文本数  : {len(set(texts))}")

    if not texts:
        print("\n  没有抓到任何文本。请确认：")
        print("    · 实时字幕已打开，且识别语言是英语（python tools/caption_language.py）")
        print("    · 声音确实从默认输出设备播放（耳机独占模式会导致实时字幕收不到）")
        return

    has_cr = sum(1 for t in texts if "\r" in t)
    has_lf = sum(1 for t in texts if "\n" in t)
    print(f"  含 \\r 的样本      : {has_cr}")
    print(f"  含 \\n 的样本      : {has_lf}")
    if has_cr and not has_lf:
        print("  → 换行符是 \\r")
    elif has_lf and not has_cr:
        print("  → 换行符是 \\n")
    elif has_cr and has_lf:
        print("  → 换行符是 \\r\\n")

    max_lines = max(len(t.splitlines()) for t in texts)
    print(f"  最大行数          : {max_lines}")

    appends = rewrites = 0
    for prev, cur in zip(texts, texts[1:]):
        if cur.startswith(prev):
            appends += 1
        else:
            rewrites += 1
    print(f"  追加式更新        : {appends}")
    print(f"  改写式更新        : {rewrites}")

    # 相邻两次变化的间隔分布，直接对应 stable_ms 该设多大
    gaps = [round(b["t"] - a["t"], 2) for a, b in zip(samples, samples[1:]) if b["raw"]]
    if gaps:
        gaps_sorted = sorted(gaps)
        median = gaps_sorted[len(gaps_sorted) // 2]
        print(f"  更新间隔 中位数   : {median}s")
        print(f"  更新间隔 最大     : {max(gaps)}s")
        print("\n  提示：captions.stable_ms 应明显大于常见更新间隔，否则残句会在中途被定稿。")
        print(f"        当前默认 700ms，实测中位间隔 {median}s → ", end="")
        print("偏小，建议调大" if median < 0.7 else "足够")

    print("\n  最后 3 条原始样本：")
    for s in samples[-3:]:
        if s["raw"]:
            print(f"    [{s['t']:6.2f}s] {s['raw']!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取实时字幕真实输出")
    parser.add_argument("--seconds", type=float, default=60.0, help="抓取时长（秒）")
    parser.add_argument("--speak", action="store_true", help="用 Windows 语音合成念一段英文（无需视频）")
    parser.add_argument("--text-file", help="改念这个文件里的英文文本")
    parser.add_argument("--out", help="保存路径（默认 tests/fixtures/real_livecaptions_<时间>.jsonl）")
    parser.add_argument("--no-save", action="store_true", help="只打印，不保存")
    args = parser.parse_args()

    reader = CaptionReader(auto_launch=True, log=lambda m: print(f"  · {m}"))
    state = None
    for _ in range(40):
        state = reader.read()
        if state.status in ("idle", "captioning", "no-element"):
            break
        time.sleep(0.5)
    print(f"实时字幕状态：{state.status}")

    speaker = None
    if args.speak or args.text_file:
        text = DEFAULT_SPEECH
        if args.text_file:
            with open(args.text_file, encoding="utf-8") as handle:
                text = handle.read().strip()
        speaker = speak_async(text)
        print("已开始播放英文语音，开始抓取 …\n")
    else:
        print("请开始播放有英文语音的视频 …\n")

    samples = capture(reader, args.seconds)

    if speaker is not None:
        try:
            speaker.wait(timeout=10)
        except Exception:
            speaker.kill()
    reader.close()

    analyse(samples)

    if not args.no_save and any(s["raw"] for s in samples):
        out_dir = os.path.join(_ROOT, "tests", "fixtures")
        os.makedirs(out_dir, exist_ok=True)
        path = args.out or os.path.join(
            out_dir, f"real_livecaptions_{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        )
        with open(path, "w", encoding="utf-8") as handle:
            for sample in samples:
                handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
        print(f"\n已保存到：{path}")

    return 0 if any(s["raw"] for s in samples) else 1


if __name__ == "__main__":
    raise SystemExit(main())

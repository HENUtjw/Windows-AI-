#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分析实时字幕录制日志：重复 / 内容丢失 / 碎片 / 翻页。

为什么要做成工具
----------------
"某一句话被翻译了两遍" 或 "有些话根本没被翻译" 这类问题，靠读代码是看不出来的——
它们只在真实语音的时序下才暴露。这个工具把日志回放一遍，直接给出可核对的数字。

用法::

    python tools/analyze_log.py                      # 分析 logs/ 里最新的一份
    python tools/analyze_log.py --log logs/xxx.log   # 指定文件
    python tools/analyze_log.py --all                # 依次分析全部

判据说明（都是踩过坑之后定下来的）
----------------------------------
* **内容覆盖必须两级判定**：只看"行开头几个词在不在提交流里"会把大量
  「开头接了识别噪声」的行误报成丢失。开头缺失时还要再看中间/结尾的特征片段，
  只有整行都找不到才算真丢失。
* **跨位置重复不能一律算 bug**：说话人确实会把 ``Right.`` ``OK.`` 说很多次。
  按提交位置的距离区分——离得远的属于正常重复语音，离得近（且一方包含另一方）
  才是改写造成的重复提交。
"""

from __future__ import annotations

import argparse
import ast
import glob
import os
import re
import sys
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from src.segmenter import (  # noqa: E402
    CaptionSegmenter,
    _signature,
    _tokens,
    split_lines,
)

_STAMP = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\]\s(.*)$")

# 实时字幕滚动窗口的行数（实测满 12 行开始翻页）
FULL_WINDOW = 12
# 两次提交相隔少于这么多句，才算「靠得近、可能是改写造成的重复」
NEAR_DISTANCE = 20


def load(path: str) -> list[tuple[int, str]]:
    """读取日志。格式：``[HH:MM:SS] 'python repr of raw'``。"""
    samples: list[tuple[int, str]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = _STAMP.match(line.rstrip("\n"))
            if not match:
                continue
            try:
                raw = ast.literal_eval(match.group(4))
            except (ValueError, SyntaxError):
                continue
            moment = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
            samples.append((moment, raw))
    return samples


def replay(samples: list[tuple[int, str]], stable_ms: int, clause_min_words: int):
    """回放日志，返回提交的句子列表。

    日志只有秒级时间戳，同一秒内可能有多条更新；这里把它们按 0.25s 均匀展开，
    尽量贴近真实的轮询节奏（轮询间隔 120ms，更新间隔约 0.3s）。
    """
    timeline: list[tuple[float, str]] = []
    prev_sec: int | None = None
    sub = 0
    for moment, raw in samples:
        if moment != prev_sec:
            prev_sec, sub = moment, 0
        timeline.append((moment + sub * 0.25, raw))
        sub += 1

    segmenter = CaptionSegmenter(stable_ms=stable_ms, clause_min_words=clause_min_words)
    commits: list[str] = []
    for moment, raw in timeline:
        commits.extend(segmenter.feed(raw, moment))
    commits.extend(segmenter.flush(timeline[-1][0] + 2))
    return commits


def analyse(path: str, stable_ms: int, clause_min_words: int, verbose: bool) -> int:
    samples = load(path)
    print("=" * 88)
    print("概览 ——", os.path.basename(path))
    print("=" * 88)
    if not samples:
        print("  没有解析到任何条目。")
        return 1

    print("  条目数      :", len(samples))
    print("  时长        : %.1f 分钟" % ((samples[-1][0] - samples[0][0]) / 60.0))
    distribution = Counter(len(split_lines(raw)) for _, raw in samples)
    print("  转录行数分布:", dict(sorted(distribution.items())))

    scrolls = 0
    prev_first: str | None = None
    for _, raw in samples:
        lines = split_lines(raw)
        if len(lines) < FULL_WINDOW:
            prev_first = None
            continue
        if prev_first is not None and lines[0] != prev_first:
            scrolls += 1
        prev_first = lines[0]
    print("  满窗口翻页  :", scrolls, "次")

    commits = replay(samples, stable_ms, clause_min_words)
    stream: list[str] = []
    for sentence in commits:
        stream.extend(_tokens(sentence))
    stream_text = " ".join(stream)
    print("  提交句子数  :", len(commits), "  词流:", len(stream))

    # ── 重复 ──────────────────────────────────────────────
    signatures = [_signature(s) for s in commits]
    adjacent = sum(1 for i in range(1, len(signatures)) if signatures[i] and signatures[i] == signatures[i - 1])
    grouped: dict[str, list[int]] = {}
    for index, signature in enumerate(signatures):
        if signature:
            grouped.setdefault(signature, []).append(index)
    repeats = {k: v for k, v in grouped.items() if len(v) > 1}

    near_repeats = []
    far_repeats = []
    for signature, positions in repeats.items():
        span = positions[-1] - positions[0]
        (near_repeats if span < NEAR_DISTANCE else far_repeats).append((signature, positions))

    print()
    print("=" * 88)
    print("重复")
    print("=" * 88)
    print("  相邻重复（紧挨着又来一次）:", adjacent, "← 应恒为 0")
    print("  跨位置重复中「离得近」的  :", len(near_repeats), "← 可能是改写造成的重复提交")
    print("  跨位置重复中「离得远」的  :", len(far_repeats), "← 多为说话人真的重复说了")
    for signature, positions in near_repeats[:8]:
        print("    ⚠ %r  #%s" % (signature[:56], ", #".join(str(i + 1) for i in positions)))
    if verbose:
        for signature, positions in far_repeats[:8]:
            print("      %r  #%s" % (signature[:56], ", #".join(str(i + 1) for i in positions)))

    # ── 碎片 ──────────────────────────────────────────────
    short = [(i, s) for i, s in enumerate(commits) if len(s.split()) < 4]
    print()
    print("=" * 88)
    print("碎片（< 4 词）")
    print("=" * 88)
    print("  数量:", len(short), " 占比: %.1f%%" % (100.0 * len(short) / max(1, len(commits))))
    for index, sentence in short[:10]:
        print("    #%-4d %r" % (index + 1, sentence[:62]))

    # ── 内容覆盖（两级判定）──────────────────────────────────
    best_of_line: dict[str, str] = {}
    for _, raw in samples:
        for line in split_lines(raw):
            words = _tokens(line)
            if len(words) < 6:
                continue
            key = " ".join(words[:4])
            if len(words) > len(_tokens(best_of_line.get(key, ""))):
                best_of_line[key] = line

    checked = [line for line in best_of_line.values() if len(_tokens(line)) >= 8]
    opening_only: list[str] = []
    suspected: list[str] = []
    for line in checked:
        words = _tokens(line)
        if " ".join(words[:4]) in stream_text:
            continue
        probes = []
        for offset in (len(words) // 3, len(words) // 2, len(words) * 2 // 3):
            piece = " ".join(words[offset : offset + 5])
            if len(piece.split()) >= 3:
                probes.append(piece)
        tail_piece = " ".join(words[-5:])
        if len(tail_piece.split()) >= 3:
            probes.append(tail_piece)
        if any(p in stream_text for p in probes):
            opening_only.append(line)
        else:
            suspected.append(line)

    print()
    print("=" * 88)
    print("内容覆盖（按「行」，>= 8 词）")
    print("=" * 88)
    print("  检查行数        :", len(checked))
    print("  开头不在提交流里:", len(opening_only) + len(suspected))
    print("    ├ 只是开头被噪声替换:", len(opening_only))
    print("    └ 判定为真丢失      :", len(suspected), "← 应恒为 0")
    for line in opening_only[:4]:
        print("      [仅差开头] %s" % line[:74])
    for line in suspected[:8]:
        print("      [疑似丢失] %s" % line[:74])

    healthy = (
        adjacent == 0
        and not near_repeats
        and not suspected
        and len(short) <= max(10, len(commits) * 0.15)
    )
    print()
    print("结论:", "健康（无重复、无内容丢失）" if healthy else "有问题，见上面标 ⚠ / [疑似丢失] 的条目")
    return 0 if healthy else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="分析实时字幕录制日志")
    parser.add_argument("--log", help="日志文件；缺省用 logs/ 里最新的一份")
    parser.add_argument("--all", action="store_true", help="依次分析全部日志")
    parser.add_argument("--stable-ms", type=int, default=1400)
    parser.add_argument("--clause-min-words", type=int, default=12)
    parser.add_argument("-v", "--verbose", action="store_true", help="也列出离得远的重复")
    args = parser.parse_args()

    if args.all:
        paths = sorted(glob.glob(os.path.join(_ROOT, "logs", "captions-*.log")))
    elif args.log:
        paths = [args.log]
    else:
        found = sorted(
            glob.glob(os.path.join(_ROOT, "logs", "captions-*.log")),
            key=os.path.getmtime,
        )
        paths = found[-1:] or []
    if not paths:
        print("没有找到日志。先跑一段视频，或指定 --log。")
        return 1

    worst = 0
    for path in paths:
        worst = max(worst, analyse(path, args.stable_ms, args.clause_min_words, args.verbose))
        print()
    return worst


if __name__ == "__main__":
    raise SystemExit(main())

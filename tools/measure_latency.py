#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""延迟测量：把「从说完话到看见译文」的耗时拆开量。

字幕的端到端延迟由两段构成，优化前必须先知道各占多少：

1. **断句延迟**（本地，占大头）：实时字幕说完一句话到程序决定「这句定稿了」
   之间的等待。可以在不花额度、不联网的情况下，用真实录制数据回放测出来。
2. **网络延迟**：请求发出到收到第一个字（首字延迟）、到整句译完（总耗时）。
   流式输出时，用户实际感知的是首字延迟。

用法::

    python tools/measure_latency.py              # 断句延迟（回放真实录制数据）
    python tools/measure_latency.py --live       # 网络延迟（真实 DeepSeek，会消耗额度）
    python tools/measure_latency.py --live --stable-ms 1000
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import os
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from src.config import Config  # noqa: E402
from src.segmenter import CaptionSegmenter  # noqa: E402

# 代表性字幕句（长度递增），用于测网络延迟
SAMPLE_SENTENCES = [
    "Choice you make has a cost.",
    "And economists call that the opportunity cost.",
    "When you watch this video, you could be doing something else instead.",
    "That trade off is the heart of the subject we are going to study together.",
]

_WORD = re.compile(r"[^\w\s]")


def _words(text: str) -> str:
    """去掉标点、统一小写，用于判断「词有没有变」。"""
    return _WORD.sub("", text).casefold()


def measure_segmentation(stable_ms: int, clause_min_words: int = 0) -> int:
    """回放真实录制数据，量出每句额外等了多久。返回退出码。"""
    files = sorted(
        glob.glob(os.path.join(_ROOT, "tests", "fixtures", "real_livecaptions_*.jsonl"))
    )
    if not files:
        print("没有找到录制数据。可以先用 tools/capture_captions.py 抓一段：")
        print("    python tools/capture_captions.py --speak --seconds 50")
        return 1

    with open(files[-1], encoding="utf-8") as handle:
        samples = [json.loads(line) for line in handle if line.strip()]
    changes = [(s["t"], s["raw"]) for s in samples]
    times = [c[0] for c in changes]

    def raw_at(moment: float) -> str:
        index = bisect.bisect_right(times, moment) - 1
        return changes[index][1] if index >= 0 else ""

    seg = CaptionSegmenter(stable_ms=stable_ms, clause_min_words=clause_min_words)
    emitted: list[tuple[float, str]] = []
    end = changes[-1][0] + 3.0
    moment = changes[0][0]
    while moment <= end:
        for sentence in seg.feed(raw_at(moment), moment):
            emitted.append((moment, sentence))
        moment += 0.12
    for sentence in seg.flush(end):
        emitted.append((end, sentence))

    print(f"回放数据：{os.path.basename(files[-1])}    stable_ms = {stable_ms}"
          f"    clause_min_words = {clause_min_words}")
    print("-" * 74)
    print(f"{'句子':<52}{'已说完':>8}{'定稿':>8}{'额外等':>8}")

    delays: list[float] = []
    for commit_time, sentence in emitted:
        key = _words(sentence)
        appear = None
        for change_time, raw in changes:
            if key and key in _words(raw):
                appear = change_time
                break
        delay = (commit_time - appear) if appear is not None else float("nan")
        if appear is not None:
            delays.append(delay)
        shown = sentence if len(sentence) <= 50 else sentence[:47] + "…"
        print(
            f"{shown:<52}{appear if appear is not None else -1:>8.2f}"
            f"{commit_time:>8.2f}{delay:>8.2f}"
        )

    print("-" * 74)
    if delays:
        average = sum(delays) / len(delays)
        print(f"额外等待：平均 {average:.2f}s   最大 {max(delays):.2f}s   共 {len(delays)} 句")
    return 0


def measure_network(cfg: Config) -> int:
    """用真实服务量首字延迟与总耗时。"""
    from src.translator import DeepSeekTranslator, TranslationError

    if not cfg.api_key:
        print("--live 需要 config.json 里配置有效的 deepseek.api_key")
        return 2

    translator = DeepSeekTranslator(cfg)
    print(f"真实服务：{cfg.get('deepseek.base_url')}   模型 {cfg.get('deepseek.model')}")
    print(f"流式输出：{cfg.get('deepseek.stream')}   上下文句数：{cfg.get('translate.context_sentences')}")
    print("-" * 74)
    print(f"{'首字':>8}{'总耗时':>9}   译文")
    firsts: list[float] = []
    totals: list[float] = []
    context: list[tuple[str, str]] = []
    try:
        for sentence in SAMPLE_SENTENCES:
            marks: dict = {}

            def on_delta(_piece: str, _m: dict = marks) -> None:
                _m.setdefault("first", time.monotonic())

            started = time.monotonic()
            try:
                result = translator.translate(sentence, context, on_delta=on_delta)
            except TranslationError as exc:
                print(f"{'--':>8}{'--':>9}   {exc}")
                return 1
            finished = time.monotonic()
            first = marks.get("first", finished)
            firsts.append(first - started)
            totals.append(finished - started)
            context.append((sentence, result))
            shown = result if len(result) <= 40 else result[:37] + "…"
            print(f"{first - started:>8.2f}{finished - started:>9.2f}   {shown}")
    finally:
        translator.close()

    print("-" * 74)
    if totals:
        print(
            f"首字延迟：平均 {sum(firsts) / len(firsts):.2f}s  "
            f"总耗时：平均 {sum(totals) / len(totals):.2f}s  "
            f"共 {len(totals)} 句"
        )
    return 0


def measure_stress(cfg: Config, workers: int, interval: float, count: int) -> int:
    """压力测试：按固定节奏喂句子，看译文滞后会不会越积越多。

    做法是直接驱动真正的 TranslatorApp（只是不读字幕、由我们自己按节奏喂），
    所以量到的是真实的多线程行为，而不是另写一个简化模型。

    判据很简单：如果每句的「滞后」从头到尾保持平稳，说明吞吐够用；
    如果持续变大，说明翻译跟不上出句速度——那正是「越看越不同步」的成因。
    """
    from src.app import TranslatorApp

    cfg.set("translate.workers", workers)
    cfg.set("overlay.enabled", False)
    cfg.set("captions.log_raw_text", False)
    cfg.set("captions.auto_launch", False)

    pool = [
        "Economics is not just about money.",
        "It studies how people use limited resources.",
        "Every choice you make has a cost.",
        "Markets are one way to coordinate all those decisions.",
        "Scarcity means we cannot have everything we want.",
    ]

    app = TranslatorApp(cfg, mock=False, demo=True)
    app._reader_loop = lambda: None  # type: ignore[assignment]  自己喂，不用字幕源
    app._start_threads()

    submit_at: dict[int, float] = {}
    done_at: dict[int, float] = {}
    original_set = app._display.set_translation

    def spy(row_id: int, text: str, done: bool) -> None:
        if done and row_id not in done_at and not text.startswith("⚠"):
            done_at[row_id] = time.monotonic()
        return original_set(row_id, text, done)

    app._display.set_translation = spy  # type: ignore[assignment]

    print(f"翻译线程数 {workers}   出句间隔 {interval:.2f}s（相当于每 {1/interval:.1f} 句/秒）"
          f"   共 {count} 句")
    print("-" * 74)
    print(f"{'序号':>4}{'提交':>9}{'完成':>9}{'滞后':>9}   ")

    started = time.monotonic()
    for index in range(count):
        sentence = pool[index % len(pool)]
        row_id = app._display.add(sentence)
        submit_at[row_id] = time.monotonic()
        app._jobs.put((row_id, sentence))
        time.sleep(interval)

    deadline = time.monotonic() + max(60.0, count * 3.0)
    while len(done_at) < count and time.monotonic() < deadline:
        time.sleep(0.05)

    # 收尾
    app._stop.set()
    for _ in range(max(1, workers)):
        app._jobs.put(None)
    app._shutdown()

    lags: list[float] = []
    for index, row_id in enumerate(sorted(submit_at), 1):
        if row_id not in done_at:
            continue
        lag = done_at[row_id] - submit_at[row_id]
        lags.append(lag)
        if index <= 5 or index > len(submit_at) - 3:
            print(f"{index:>4}{submit_at[row_id] - started:>9.2f}"
                  f"{done_at[row_id] - started:>9.2f}{lag:>9.2f}")

    if not lags:
        print("没有拿到任何译文，无法判断。")
        return 1

    head = lags[:3]
    tail = lags[-3:]
    average = sum(lags) / len(lags)
    print("-" * 74)
    print(f"完成 {len(lags)}/{count} 句   滞后：平均 {average:.2f}s   最大 {max(lags):.2f}s")
    print(f"前 3 句平均 {sum(head)/len(head):.2f}s   最后 3 句平均 {sum(tail)/len(tail):.2f}s")
    if sum(tail) / len(tail) > sum(head) / len(head) * 2 + 1.0:
        print("→ 滞后在持续累积：翻译跟不上出句速度，字幕会越看越不同步。")
        return 2
    print("→ 滞后保持平稳：吞吐够用，不会越积越多。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="测量字幕翻译的延迟构成")
    parser.add_argument("--live", action="store_true", help="用真实 DeepSeek 服务测网络延迟（耗额度）")
    parser.add_argument("--stable-ms", type=int, default=None, help="覆盖断句稳定阈值")
    parser.add_argument(
        "--stress",
        action="store_true",
        help="压力测试：按固定节奏喂句子，看滞后会不会累积（用真实服务，耗额度）",
    )
    parser.add_argument("--workers", type=int, default=None, help="压力测试用的翻译线程数")
    parser.add_argument("--interval", type=float, default=1.5, help="压力测试的出句间隔（秒）")
    parser.add_argument("--count", type=int, default=12, help="压力测试喂多少句")
    parser.add_argument(
        "--clause-min-words",
        type=int,
        default=None,
        help="逗号从句门槛（0 = 关闭）。用于对比长句提速效果",
    )
    parser.add_argument("--config", help="配置文件路径")
    args = parser.parse_args()

    cfg = Config.load(args.config)
    if args.stable_ms is not None:
        cfg.set("captions.stable_ms", args.stable_ms)
    if args.clause_min_words is not None:
        cfg.set("captions.clause_min_words", args.clause_min_words)

    if args.stress:
        return measure_stress(
            cfg,
            workers=args.workers or int(cfg.get("translate.workers", 3)),
            interval=args.interval,
            count=args.count,
        )
    if args.live:
        return measure_network(cfg)
    return measure_segmentation(
        int(cfg.get("captions.stable_ms", 1400)),
        int(cfg.get("captions.clause_min_words", 0)),
    )


if __name__ == "__main__":
    raise SystemExit(main())

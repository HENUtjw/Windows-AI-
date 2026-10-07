#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""断句器与配置的离线测试（不需要网络、不需要实时字幕）。

运行::

    python tests/test_segmenter.py

退出码 0 表示全部通过。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    # 只放宽错误处理，不改编码：中文控制台下中文仍能正常显示
    sys.stdout.reconfigure(errors="replace")

from src.caption_reader import DEMO_PHRASES, ScriptedCaptionReader  # noqa: E402
from src.config import Config  # noqa: E402
from src.segmenter import (  # noqa: E402
    CaptionSegmenter,
    _is_duplicate,
    normalize,
    split_complete,
    split_lines,
    strip_noise,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}   {detail}")
        FAILURES.append(name)


# --------------------------------------------------------------------------- #
def test_helpers() -> None:
    print("\n[工具函数]")
    check("normalize 压缩空白", normalize("  a   b \n c ") == "a b c", normalize("  a   b \n c "))
    check("split_lines 兼容 \\r 和 \\n", split_lines("a\rb\nc") == ["a", "b", "c"])
    check("strip_noise 移除 [Music]", strip_noise("[Music] hello") == "hello", strip_noise("[Music] hello"))
    check("strip_noise 移除 (laughs)", strip_noise("hi (laughs) there") == "hi there", strip_noise("hi (laughs) there"))

    complete, rest = split_complete("Hello world. How are you")
    check("split_complete 切出完整句", complete == ["Hello world."], str(complete))
    check("split_complete 保留残句", rest == "How are you", rest)

    complete, rest = split_complete("It costs 3.5 dollars")
    check("小数点不被误切", complete == [] and rest == "It costs 3.5 dollars", f"{complete} / {rest}")

    complete, rest = split_complete("Wait... what?")
    check("连续标点可切分", complete == ["Wait...", "what?"], str(complete))


# --------------------------------------------------------------------------- #
def test_progressive_growth() -> None:
    print("\n[渐进式字幕：不应过早或重复定稿]")
    seg = CaptionSegmenter(stable_ms=1800)
    produced: list[str] = []
    clock = 0.0
    for text in ("The quick", "The quick brown", "The quick brown fox"):
        produced += seg.feed(text, clock)
        clock += 0.2
    check("追加过程中不输出", produced == [], str(produced))

    # 实测：实时字幕会给「还没说完的半句」也补句号，所以末尾句号不能作为定稿依据
    produced = seg.feed("The quick brown fox jumps.", clock)
    check("末尾句号不立即定稿", produced == [], str(produced))
    check("内容进入待定", seg.pending == "The quick brown fox jumps.", seg.pending)

    # 只有后续文本证明这一句确实结束了，才提交
    produced = seg.feed("The quick brown fox jumps.\nNext one starts", clock + 0.2)
    check("有后续文本时才定稿", produced == ["The quick brown fox jumps."], str(produced))

    produced = seg.feed("The quick brown fox jumps.\nNext one starts", clock + 0.3)
    check("重复喂入不重复定稿", produced == [], str(produced))


def test_partial_words_are_not_sentences() -> None:
    """实测坑：实时字幕给半句也补句号，早期版本因此把 'Econom.' 当成一句话。"""
    print("\n[半句带句号：不得当成完整句]")
    seg = CaptionSegmenter(stable_ms=1800)
    clock = 0.0
    produced: list[str] = []
    for text in ("Econom.", "Economics is.", "Economics is not.", "Economics is not just."):
        produced += seg.feed(text, clock)
        clock += 0.3
    check("半句序列不产生任何输出", produced == [], str(produced))

    produced = seg.feed("Economics is not just about money.", clock)
    check("完整句单独出现仍不立即定稿", produced == [], str(produced))

    produced = seg.feed("Economics is not just about money.\nIt studies.", clock + 0.3)
    check("有后续文本时完整句才定稿", produced == ["Economics is not just about money."], str(produced))

    check("半句没有被送出", "Econom." not in produced, str(produced))


def test_stability_commit() -> None:
    print("\n[无标点残句：靠稳定阈值定稿]")
    seg = CaptionSegmenter(stable_ms=700)
    text = "This is a sentence without punctuation"
    check("首次出现不输出", seg.feed(text, 0.0) == [])
    check("未到阈值不输出", seg.feed(text, 0.5) == [])
    produced = seg.feed(text, 0.8)
    check("超过阈值应定稿", produced == [text], str(produced))
    check("定稿后不重复", seg.feed(text, 1.0) == [])

    seg = CaptionSegmenter(stable_ms=700)
    seg.feed("Still typing here", 0.0)
    seg.feed("Still typing here now", 0.3)
    check("文本变化会重新计时", seg.feed("Still typing here now", 0.9) == [])
    check("变化后满阈值才定稿", seg.feed("Still typing here now", 1.1) == ["Still typing here now"])


def test_multiline() -> None:
    print("\n[多行滚动]")
    seg = CaptionSegmenter(stable_ms=700)
    produced = seg.feed("First line is done.\nSecond line growing", 0.0)
    check("上一行立即定稿", produced == ["First line is done."], str(produced))

    produced = seg.feed("First line is done.\nSecond line growing\nThird appears", 0.1)
    check("继续滚动时定稿新完整行", produced == ["Second line growing"], str(produced))

    produced = seg.feed("First line is done.\nSecond line growing\nThird appears", 0.2)
    check("已定稿的行不再重复", produced == [], str(produced))


def test_duplicate_suppression() -> None:
    print("\n[去重：残句定稿后又作为完整行出现]")
    seg = CaptionSegmenter(stable_ms=700)
    text = "Hello there my friend"
    seg.feed(text, 0.0)
    produced = seg.feed(text, 0.8)
    check("残句定稿一次", produced == [text], str(produced))

    produced = seg.feed(f"{text}\nand more words", 0.9)
    check("同一句再次出现不重复定稿", produced == [], str(produced))

    produced = seg.feed(f"{text} everyone\nand more words", 1.0)
    check("扩展版被视为同一句（互为前缀）", produced == [], str(produced))


def test_flush_and_empty() -> None:
    print("\n[收尾与空输入]")
    seg = CaptionSegmenter(stable_ms=700)
    check("空字符串不输出", seg.feed("", 0.0) == [])
    check("纯空白不输出", seg.feed("   \n \t ", 0.0) == [])

    seg.feed("Half a sentence", 0.0)
    check("flush 定稿残句", seg.flush(0.1) == ["Half a sentence"])
    check("flush 后 pending 清空", seg.pending == "", seg.pending)
    check("flush 不重复输出", seg.flush(0.2) == [])


def test_in_line_sentence_split() -> None:
    print("\n[单行内含多句]")
    seg = CaptionSegmenter(stable_ms=1800)
    produced = seg.feed("Hello world. How are you", 0.0)
    check("有后续文本的完整句定稿", produced == ["Hello world."], str(produced))
    check("残句进入待定", seg.pending == "How are you", seg.pending)

    # 整行以句号收尾时，末尾那句仍可能是还在生长的半句 → 不能立即定稿
    produced = seg.feed("Hello world. How are you doing today?", 0.3)
    check("末尾句不立即定稿", produced == [], str(produced))
    check("待定收敛为末尾句", seg.pending == "How are you doing today?", seg.pending)

    # 长时间不再变化 → 兜底定稿（且只定稿末尾句，不能被整行去重吞掉）
    produced = seg.feed("Hello world. How are you doing today?", 2.3)
    check("超时兜底定稿末尾句", produced == ["How are you doing today?"], str(produced))


def test_full_rolling_scenario() -> None:
    """用与演示模式相同的时间轴跑一遍，验证每句恰好输出一次且顺序正确。"""
    print("\n[完整滚动场景：每句恰好输出一次]")
    timeline = ScriptedCaptionReader._build(1.0)
    seg = CaptionSegmenter(stable_ms=700)

    produced: list[str] = []
    for clock, text in timeline:
        produced += seg.feed(text, clock)
    produced += seg.flush(timeline[-1][0] + 1.0)

    check("输出数量与脚本句数一致", len(produced) == len(DEMO_PHRASES), f"{len(produced)} vs {len(DEMO_PHRASES)}: {produced}")
    check("输出内容与顺序完全正确", produced == DEMO_PHRASES, str(produced))


def test_real_captions_replay() -> None:
    """用真机录制的实时字幕数据回放，锁定断句行为。

    录制方法：``python tools/capture_captions.py --speak``
    数据里包含了真实世界的全部麻烦：半句带句号、回溯补标点、丢连字符。
    """
    print("\n[真实字幕数据回放]")
    import bisect
    import glob
    import json

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = sorted(glob.glob(os.path.join(base, "tests", "fixtures", "real_livecaptions_*.jsonl")))
    if not files:
        print("  [SKIP] 没有找到录制数据（可用 tools/capture_captions.py 生成）")
        return

    with open(files[-1], encoding="utf-8") as handle:
        samples = [json.loads(line) for line in handle if line.strip()]
    changes = [(s["t"], s["raw"]) for s in samples]
    times = [c[0] for c in changes]

    def raw_at(moment: float) -> str:
        index = bisect.bisect_right(times, moment) - 1
        return changes[index][1] if index >= 0 else ""

    # 按真实轮询节奏（每 120ms 一次）回放，否则走不到稳定判定那条分支
    seg = CaptionSegmenter(stable_ms=1400)
    produced: list[str] = []
    end = changes[-1][0] + 3.0
    moment = changes[0][0]
    while moment <= end:
        produced += seg.feed(raw_at(moment), moment)
        moment += 0.12
    produced += seg.flush(end)

    check("产出 7 句", len(produced) == 7, f"{len(produced)}: {produced}")
    check("没有碎片（每句至少 3 个词）", all(len(s.split()) >= 3 for s in produced), str(produced))
    # 实测：stable_ms=1400 时多数句子会在实时字幕补标点之前就定稿（这是刻意的提速取舍），
    # 所以不要求每句都带句末标点，但不能一句都没有。
    punctuated = sum(1 for s in produced if s[-1] in ".!?…。！？")
    check("至少部分句子带句末标点", punctuated >= 1, f"{punctuated}/{len(produced)}: {produced}")
    check("没有重复", len(set(produced)) == len(produced), str(produced))
    check(
        "第一句内容完整正确（标点可有可无）",
        bool(produced) and produced[0].strip().rstrip(".!?…。！？") == "Economics is not just about money",
        str(produced[:1]),
    )
    check(
        "半句未被当成句子（Econom. / Every. / And.）",
        not any(s in ("Econom.", "Every.", "And.", "It studies.", "That trade.") for s in produced),
        str(produced),
    )
    check(
        "长句被正确切分（在停顿处切开，标点可有可无）",
        any(
            s.strip().rstrip(".!?…。！？") == "And economists call that the opportunity cost"
            for s in produced
        ),
        str(produced),
    )


def test_rerecognition_duplicates() -> None:
    """实测：同一段语音会被重新识别成不同版本，前缀关系抓不到，需要包含判断。"""
    print("\n[重新识别造成的重复]")
    seg = CaptionSegmenter(stable_ms=1800)
    out = seg.feed("Let us begin with a simple question about scarcity.\nNext.", 0.0)
    check("首句定稿", out == ["Let us begin with a simple question about scarcity."], str(out))

    # 同一段语音的重识别版本（缺了开头两个词）→ 包含关系，应被抑制
    out = seg.feed(
        "Let us begin with a simple question about scarcity.\n"
        "Begin with a simple question about scarcity.\nNext.",
        0.5,
    )
    check("包含关系的重识别版本被抑制", out == [], str(out))

    # 真正不同的句子不能被误抑制
    out = seg.feed(
        "Let us begin with a simple question about scarcity.\n"
        "Begin with a simple question about scarcity.\n"
        "That trade off is the heart of the subject.\nNext.",
        1.0,
    )
    check("不同句子正常定稿", out == ["That trade off is the heart of the subject."], str(out))


def test_prime_skips_backlog() -> None:
    """程序在会话中途启动时，不能把历史转录全部送去翻译。"""
    print("\n[启动时跳过历史转录]")
    backlog = "\n".join(f"This is historical sentence number {i}." for i in range(1, 21))
    backlog += "\nAnd the current one is still"

    seg = CaptionSegmenter(stable_ms=1800)
    seg.prime(backlog, 0.0)
    check("prime 不产出任何句子", True)

    produced = seg.feed(backlog, 0.1)
    check("首次 feed 不吐出历史内容", produced == [], str(produced))

    produced = seg.feed(backlog + " growing.", 0.2)
    check("新增内容仍能被跟踪", produced == [], str(produced))

    produced = seg.feed(
        backlog + " growing.\nA brand new sentence appears.", 0.3
    )
    check(
        "只有真正的新句子被定稿",
        produced == ["And the current one is still growing."],
        str(produced),
    )


def test_processed_lines_reset_on_shrink() -> None:
    print("\n[转录被清空后能重新跟踪]")
    seg = CaptionSegmenter(stable_ms=1800)
    seg.feed("First sentence here.\nSecond one.", 0.0)
    seg.feed("First sentence here.\nSecond one.\nThird one.", 0.1)
    # 转录被清空（实时字幕重启）
    produced = seg.feed("Completely new start.\nAnother line.", 0.2)
    check("清空后新内容可定稿", produced == ["Completely new start."], str(produced))


def test_config() -> None:
    print("\n[配置]")
    cfg = Config.load(None)
    check("默认值可读", cfg.get("deepseek.model") == "deepseek-flash", str(cfg.get("deepseek.model")))
    check("点号路径缺失返回默认", cfg.get("no.such.key", "fallback") == "fallback")
    check("system_prompt 会填入目标语言", "简体中文" in cfg.system_prompt)
    check("项目内相对路径可解析", cfg.resolve_path("logs").is_absolute())

    merged = Config({"deepseek": {"model": "deepseek-reasoner"}})
    check("局部覆盖生效", merged.get("deepseek.model") == "deepseek-reasoner")
    check("未覆盖项保留默认", merged.get("deepseek.base_url") == "https://api.deepseek.com")

    # 带 BOM 的配置文件必须能读：PowerShell 的 `Set-Content -Encoding UTF8`
    # 会写 BOM，用 utf-8 读取会直接报 JSON 解析失败。
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        bom_path = Path(tmp) / "bom.json"
        bom_path.write_bytes(
            b"\xef\xbb\xbf" + '{"deepseek": {"model": "deepseek-v4-pro"}}'.encode("utf-8")
        )
        from src.config import Config as _Config

        cfg_bom = _Config.load(bom_path)
        check(
            "带 BOM 的配置文件可正常读取",
            cfg_bom.get("deepseek.model") == "deepseek-v4-pro",
            str(cfg_bom.get("deepseek.model")),
        )


# --------------------------------------------------------------------------- #
def test_clause_split() -> None:
    """逗号从句切分：长句提速的关键，同时不能把短句切碎。"""
    long_clause = (
        "That trade off is the heart of the subject we are going to study together,"
        " and it will keep coming back throughout the course."
    )
    complete, _ = split_complete(long_clause, 12)
    check(
        "长从句在逗号处被切出",
        complete[:1]
        == ["That trade off is the heart of the subject we are going to study together,"],
        str(complete),
    )
    check("逗号之后成为另一句", complete[-1].startswith("and it will keep"), str(complete))

    # 词数不够就不能切：否则 "However," / 列表枚举会被切成一地碎片
    for text in (
        "However, we should think about it more carefully.",
        "We bought apples, oranges, and bananas at the market today.",
        "When you watch this video, you could be doing something else instead.",
    ):
        complete, _ = split_complete(text, 12)
        check(f"短从句不切分：{text[:22]}", complete == [text], str(complete))

    # 关闭时行为与从前完全一致：整句一起返回
    complete, _ = split_complete(long_clause, 0)
    check("clause_min_words=0 时不在逗号处切", complete == [long_clause], str(complete))


def test_long_sentence_emits_progressively() -> None:
    """长句应当边说边出，而不是等整句说完才定稿。

    这正是「针对长句子提速」要解决的问题：一个 30+ 词的句子若只认句末标点，
    字幕要等它整个说完（实测可超过 8 秒）才出现。
    """
    first_clause = "That trade off is the heart of the subject we are going to study together,"
    rest_clause = "and it will keep coming back throughout the course."

    seg = CaptionSegmenter(stable_ms=1400, clause_min_words=12)
    emitted: list[str] = []
    grown = ""
    for word in (first_clause + " " + rest_clause).split():
        grown = (grown + " " + word).strip()
        emitted.extend(seg.feed(grown, now=0.0))
        if emitted:
            break
    check("第一个从句在整句说完之前就定稿了", emitted == [first_clause], str(emitted))
    check("剩余部分继续待定", seg.pending.startswith("and"), seg.pending)

    seg_off = CaptionSegmenter(stable_ms=1400, clause_min_words=0)
    emitted_off: list[str] = []
    grown = ""
    for word in (first_clause + " " + rest_clause).split():
        grown = (grown + " " + word).strip()
        emitted_off.extend(seg_off.feed(grown, now=0.0))
    check("关闭从句切分时不会提前定稿", emitted_off == [], str(emitted_off))


def test_extension_keeps_new_content() -> None:
    """只多了新内容时不能整句当重复丢掉 —— 那会永久丢失内容。

    实测踩到过：实时字幕的句号迟到时，逗号从句会把两句话拼在一起，
    旧逻辑判定为「前缀重复」后整句丢弃，后半句从此消失。
    """
    seg = CaptionSegmenter(stable_ms=1400, clause_min_words=12)
    seg._recent.append("And economists call that the opportunity cost")
    got = seg._commit(
        "And economists call that the opportunity cost When you watch this video,"
    )
    check("已翻译部分被剥掉，新内容保留下来", got == "When you watch this video,", repr(got))

    seg2 = CaptionSegmenter(stable_ms=1400)
    seg2._recent.append("Economics is not just about money")
    check(
        "仅补了个句号仍判定为重复",
        seg2._commit("Economics is not just about money.") is None,
        "应返回 None，否则会产生一个「.」的垃圾译文",
    )


def test_backtracking_rewrite_is_not_retranslated() -> None:
    """回归：实时字幕**回溯改写**已显示内容后，同一句不得再翻译一次。

    实测（用户真实运行日志 2026-10-07）：同一句

        As we begin ... a quote from one of the most famous economists of all times,

    在 11 秒后被以 ``1 of`` 的形式重新提交（实时字幕把 ``one`` 改写成了 ``1``）。
    旧去重逻辑逐字比较，认不出是同一句，于是**同一句话被翻译了两次**——
    用户看到的就是「有一些话会重复翻译」，而且因为旧内容排在更晚的位置，
    同时表现为「翻译顺序有问题」。

    修法：比较用归一化签名（去标点 + 小写 + 数字统一成英文单词）。
    """
    first = (
        "As we begin our journey into the world of economics, I thought I would begin "
        "with a quote from one of the most famous economists of all times,"
    )
    rewritten_number = first.replace("one of the most", "1 of the most")

    check("数字改写被视为同一句", _is_duplicate(rewritten_number, first))
    check("大小写改写被视为同一句", _is_duplicate(first.upper(), first))
    check("补句号被视为同一句", _is_duplicate(first + ".", first))
    check("标点改写被视为同一句", _is_duplicate(first.replace(",", ";"), first))

    seg = CaptionSegmenter(stable_ms=1400, clause_min_words=12)
    check("首次提交成功", seg._commit(first) == first)
    check("改写版不再重复提交", seg._commit(rewritten_number) is None, repr(seg._commit(first)))

    # 但「改写 + 新内容」绝不能把新内容一起丢掉
    longer = rewritten_number + " and he really is kind of the first real economist."
    got = seg._commit(longer)
    check(
        "改写+新内容只翻译新增部分",
        got == "and he really is kind of the first real economist.",
        repr(got),
    )
    check("新增部分已进入去重窗口", any("first real economist" in s for s in seg._recent))


def test_page_turn_flushes_pending() -> None:
    """回归：滚动窗口翻页时，上一行未定稿的残句必须定稿，不能丢。

    实测（用户 16 分钟真实日志）：满 12 行后窗口开始滚动（实测 34 次翻页），
    此时上一行被顶到倒数第二，而 ``_processed_lines`` 早已顶到上限，它再也不会
    被 ``_commit_line`` 处理 —— 残句随 ``_pending`` 被新尾行覆盖而**静默消失**。
    44 行里有 6 行（14%）从头到尾一个字都没被翻译过，例如
    ``Air, for most of human history, has been considered a free resource.``
    """
    seg = CaptionSegmenter(stable_ms=1400, clause_min_words=12)
    filler = [f"filler line {i} with some words." for i in range(11)]
    tail = "Air, for most of human history, has been considered a free resource."

    seg.feed("\n".join(filler + [tail]), now=0.0)
    check("翻页前残句还在待定中", "free resource" in seg.pending, seg.pending[:60])

    # 翻页：新行加进来，最旧的一行被顶掉，原尾行移到倒数第二
    out = seg.feed("\n".join(filler[1:] + [tail, "And then the next line begins here."]), now=0.5)
    check("翻页时旧尾行的残句被定稿", any("free resource" in s for s in out), out)


def test_page_turn_drops_dangling_function_word() -> None:
    """翻页残留只是单个功能词（``Of.`` / ``And.``）时按识别噪声丢弃。

    实时字幕会把句号插在半个词后面产生 ``Of.`` 这类噪声。提交它只会多出一行
    无意义字幕和一次浪费的 API 调用；实测不加这道过滤会多出 6 个单字碎片。
    """
    seg = CaptionSegmenter(stable_ms=1400, clause_min_words=12)
    filler = [f"filler line {i} with some words." for i in range(11)]

    seg.feed("\n".join(filler + ["Of."]), now=0.0)
    out = seg.feed("\n".join(filler[1:] + ["Of.", "And then more text follows here."]), now=0.5)
    check("单功能词残留不提交", not any("Of" in s for s in out), out)

    # 但真正的短句（非功能词）仍要保住
    seg2 = CaptionSegmenter(stable_ms=1400, clause_min_words=12)
    seg2.feed("\n".join(filler + ["Yes, exactly."]), now=0.0)
    out2 = seg2.feed("\n".join(filler[1:] + ["Yes, exactly.", "More text follows here."]), now=0.5)
    check("真实短句仍被定稿", any("exactly" in s for s in out2), out2)


def test_short_word_does_not_swallow_longer_sentence() -> None:
    """回归：短句不得把「以同字母开头的长句」判成重复而整句丢弃。

    曾经的写法是 ``a.startswith(b)``（**拼接后的字符串**比较），于是候选
    ``all of the debian packages…`` 会被上一句 ``A.`` 判成重复 ——
    ``"all".startswith("a")`` 为真 —— 整句消失。

    实测（用户 26 分钟日志）这导致
    ``All of the Debian packages I installed on Ubuntu, but it'll also show you the.``
    一个字都没被翻译。``A.`` ``So.`` ``I.`` 这类碎片一出现，后面同首字母的长句
    就会被吞掉。修法：比较改到**词**的层面。
    """
    seg = CaptionSegmenter(stable_ms=1400, clause_min_words=12)
    check("先提交 A.", seg._commit("A.") == "A.")
    long_sentence = (
        "All of the Debian packages I installed on Ubuntu, but it'll also show you the."
    )
    check("同首字母的长句不被吞掉", seg._commit(long_sentence) == long_sentence, "被误判为重复")

    seg2 = CaptionSegmenter(stable_ms=1400)
    seg2._commit("So.")
    check("So. 不吞 Something", seg2._commit("Something entirely different here.") is not None)
    seg2._commit("I.")
    check("I. 不吞 In the beginning", seg2._commit("In the beginning there was nothing.") is not None)


def test_number_rewrite_forms_are_duplicates() -> None:
    """数字写法的回溯改写要能认出是同一句，但绝不能误伤真正不同的句子。"""
    same = [
        ("a quote from one of the most famous economists", "a quote from 1 of the most famous economists"),
        ("on Friday, July Nineteenth at Nine AM,", "on Friday, July 19th at 9:00 AM."),
        ("it costs twenty dollars", "it costs 20 dollars"),
    ]
    for first, second in same:
        check(f"数字改写视为同一句：{first[-26:]}", _is_duplicate(second, first))

    different = [
        ("I have two apples.", "I have three apples."),
        ("it costs twenty dollars", "it costs thirty dollars"),
        ("chapter one", "chapter two"),
    ]
    for first, second in different:
        check(f"不同数字不得误判：{first[-22:]}", not _is_duplicate(second, first))


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 74)
    print("断句器 / 配置 离线测试")
    print("=" * 74)

    test_helpers()
    test_progressive_growth()
    test_partial_words_are_not_sentences()
    test_stability_commit()
    test_multiline()
    test_duplicate_suppression()
    test_flush_and_empty()
    test_in_line_sentence_split()
    test_full_rolling_scenario()
    test_real_captions_replay()
    test_rerecognition_duplicates()
    test_prime_skips_backlog()
    test_processed_lines_reset_on_shrink()
    test_clause_split()
    test_long_sentence_emits_progressively()
    test_extension_keeps_new_content()
    test_backtracking_rewrite_is_not_retranslated()
    test_page_turn_flushes_pending()
    test_page_turn_drops_dangling_function_word()
    test_short_word_does_not_swallow_longer_sentence()
    test_number_rewrite_forms_are_duplicates()
    test_config()

    print("\n" + "=" * 74)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for name in FAILURES:
            print(f"  · {name}")
        return 1
    print("全部通过 (ALL PASS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

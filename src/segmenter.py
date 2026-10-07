#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""断句与稳定判定。

真实数据实测结论（Windows 11 build 26200，英语模型，2026-10 采集）
----------------------------------------------------------------
用 ``tools/capture_captions.py`` 抓下真实字幕后的关键发现，全部影响这里的算法：

1. **``CaptionsTextBlock.Name`` 返回整场会话累积的完整转录**，不是窗口上可见的
   那两行。实测最后一次采样有 6 行且仍在增长，所以「除最后一行外都已定格」
   这个模型是成立的，但文本长度会持续增长。
2. 换行符是 ``\\n``（不是 ``\\r``）。
3. **实时字幕会给「还没说完的半句」也补上句号**：::

       'Econom.' → 'Economics is.' → 'Economics is not.' → 'Economics is not just.'
       → 'Economics is not just about' → 'Economics is not just about money'
       → 'Economics is not just about money.'

   也就是说，**句末标点并不代表句子结束**，它只是「当前识别到的末尾」。
   早期版本凭句末标点立即定稿，结果把 ``Econom.`` 当成一句完整的话送进翻译，
   随后真正的 ``Economics is not just about money.`` 因为「以前者开头」被去重规则
   误判成重复而丢弃——6 句真实内容只剩 7 个碎片。
4. 词级更新间隔中位数约 0.31s；**真句尾到补上标点之间固定间隔约 1.61s**；
   实测最大间隔 2.34s。因此 ``stable_ms`` 必须大于 1.61s，否则会把半句定稿。
5. 更新中约一半是「改写」而非「追加」：识别器会回溯插入逗号/句号、调整大小写、
   甚至丢掉连字符（``trade-off`` → ``trade off``）。

据此确定的策略
--------------
* **最后一行是「仍在生长」的，不能凭句末标点定稿。**
  只有当最后一行里某个完整句后面**还有后续文本**（说明它确实结束了）时才提交；
  否则整行留作待定。
* **非最后一行已经定格**，按标点切分后全部提交（一行里可能有多句）。
* 待定内容若 ``stable_ms`` 毫秒内不再变化，也定稿——这是说话中断时的兜底。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# 句末标点。中文标点也列入，防止源语言识别成中文时无法断句。
TERMINAL_PUNCTUATION = ".!?…。！？；;"

# 语音识别偶尔会插入的停顿标记，需要清掉
_NOISE_PATTERNS = (
    re.compile(r"\[[^\]]{0,40}\]"),  # [Music] / [Applause]
    re.compile(r"\([^)]{0,40}\)"),   # (laughs)
)

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """压缩空白，去掉首尾空格。"""
    return _WHITESPACE.sub(" ", (text or "").replace("\u00a0", " ")).strip()


def split_lines(raw: str) -> list[str]:
    """实时字幕用 \\n 分隔行（实测），同时兼容 \\r。"""
    return [line for line in re.split(r"[\r\n]+", raw or "") if line.strip()]


def strip_noise(text: str) -> str:
    """移除 [Music] 之类的非语音标记，只保留可翻译内容。"""
    for pattern in _NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    return normalize(text)


def split_complete(text: str, min_clause_words: int = 0) -> tuple[list[str], str]:
    """按句末标点切分。

    返回 ``(完整句列表, 尾部残句)``。只有当句末标点后面是空格或文本结尾时
    才算断句，这样 ``3.5`` ``Mr.`` 之类不会被误切。

    ``min_clause_words`` > 0 时，**逗号**也被当作从句边界（仅当前面已有这么多
    词）。这是给**长句提速**用的：一个 40 词的句子如果只认句末标点，就要等整句
    说完才定稿——实测等待可超过 8 秒，而且「末尾词稳定」的推测翻译永远不会触发。
    允许在逗号处提前定稿，字幕就能边说边出。

    词数门槛是关键：没有它，``However,`` ``apples, oranges,`` 这类会被切成一地碎片。

    .. warning::
       返回的「完整句」**不保证是真句子**。实测中 ``Econom.`` / ``Economics is.``
       这类半句同样以句号结尾，调用方必须结合上下文判断（见 :meth:`CaptionSegmenter.feed`）。
    """
    complete: list[str] = []
    buffer = ""
    for index, char in enumerate(text):
        buffer += char
        following = text[index + 1] if index + 1 < len(text) else ""
        if not (following == "" or following.isspace()):
            continue
        if char in TERMINAL_PUNCTUATION:
            sentence = buffer.strip()
            if sentence:
                complete.append(sentence)
            buffer = ""
        elif char == "," and min_clause_words > 0 and len(buffer.split()) >= min_clause_words:
            sentence = buffer.strip()
            if sentence:
                complete.append(sentence)
            buffer = ""
    return complete, buffer.strip()


_PUNCT_STRIP = re.compile(r"[^\w\s]")

# 时钟格式：9:00 -> 9（说话人可能说 "nine" 也可能字幕写成 "9:00"）
_CLOCK = re.compile(r"\b(\d{1,2}):\d{2}\b")
# 序数数字：19th -> 19
_ORDINAL_DIGIT = re.compile(r"(\d+)(?:st|nd|rd|th)\Z")

# 数字统一到「数字」写法，用于「是不是同一句」的比较。
# 必须逐词 1:1 映射（不能把多个数字词合并成一个），否则 _extension_remainder
# 里「按词数切出新增部分」的对齐就会错位。
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90",
}

# 序数词统一成数字：nineteenth -> 19（与 "19th" 归一化后一致）
_ORDINAL_WORDS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
    "eleventh": "11", "twelfth": "12", "thirteenth": "13", "fourteenth": "14",
    "fifteenth": "15", "sixteenth": "16", "seventeenth": "17",
    "eighteenth": "18", "nineteenth": "19", "twentieth": "20",
    "thirtieth": "30", "fortieth": "40", "fiftieth": "50", "sixtieth": "60",
}


def _tokens(text: str) -> list[str]:
    """比较用的词序列：去标点、小写、**数字统一成同一种写法**。

    为什么必须统一数字
    ------------------
    实测（用户真实日志）实时字幕会**回溯改写已经显示过的内容**，包括数字写法：

    * ``one of`` ↔ ``1 of``
    * ``July Nineteenth at Nine AM`` ↔ ``July 19th at 9:00 AM``
    * ``Seventeen seventy six`` ↔ ``1776``（**这一种尚未覆盖**，见下）

    程序在前一种写法上已经定稿翻译过，改写后同一句变成另一种写法 ——
    逐字比较认不出是同一句，于是**同一句话被翻译两遍**（用户报的
    「有一些话会重复翻译」），而且因为旧内容排在更晚的位置，同时表现为
    「翻译顺序有问题」。

    .. note::
       ``seventeen seventy six`` → ``1776`` 这种**多位数字的拼读**还没覆盖：
       它要把多个词合并成一个数字，而本函数必须保持逐词 1:1（否则
       :func:`_extension_remainder` 按词数切新增部分会错位）。宁可少认一种，
       也不能破坏切分对齐。

    另外：只去掉标点字符、不拆分连字符，这样 ``trade-off`` 仍是一个词，
    与 ``split()`` 的词数一一对应。
    """
    # 时钟格式要在去标点**之前**处理，否则 "9:00" 会先变成 "900"
    cleaned = _CLOCK.sub(r"\1", text or "")
    cleaned = _PUNCT_STRIP.sub("", cleaned.casefold())
    tokens: list[str] = []
    for token in cleaned.split():
        ordinal = _ORDINAL_DIGIT.match(token)
        if ordinal:
            tokens.append(str(int(ordinal.group(1))))
        elif token in _ORDINAL_WORDS:
            tokens.append(_ORDINAL_WORDS[token])
        elif token in _NUMBER_WORDS:
            tokens.append(_NUMBER_WORDS[token])
        elif token.isdigit():
            tokens.append(str(int(token)))
        else:
            tokens.append(token)
    return tokens


def _signature(text: str) -> str:
    return " ".join(_tokens(text))


# 翻页时若残留只有这一个词，判为识别噪声而不是内容（见 CaptionSegmenter.feed 的 2a）
_DANGLING_WORDS = {
    "of", "and", "but", "the", "to", "a", "an", "in", "on", "at", "for",
    "or", "that", "with", "as", "is", "was", "it", "this", "these", "those",
}


def _is_dangling(text: str) -> bool:
    """判断翻页残留是不是「单个功能词」这种识别噪声。"""
    words = _tokens(text)
    return len(words) == 1 and words[0] in _DANGLING_WORDS


def _extension_remainder(sentence: str, previous: str) -> str | None:
    """若 ``sentence`` 只是在 ``previous`` 的内容后面接了新内容，返回新增的原文。

    返回 ``None`` 表示不是「延长」关系。返回空串表示只是改写、没有新内容。
    """
    previous_tokens = _tokens(previous)
    current_tokens = _tokens(sentence)
    if not previous_tokens or len(current_tokens) <= len(previous_tokens):
        return None
    if current_tokens[: len(previous_tokens)] != previous_tokens:
        return None
    raw = sentence.split()
    if len(raw) <= len(previous_tokens):
        return None
    return " ".join(raw[len(previous_tokens) :]).strip()


def _is_token_prefix(shorter: list[str], longer: list[str]) -> bool:
    return bool(shorter) and len(shorter) <= len(longer) and longer[: len(shorter)] == shorter


def _contains_tokens(longer: list[str], shorter: list[str]) -> bool:
    """shorter 是否作为**连续词序列**出现在 longer 里。"""
    size = len(shorter)
    if size == 0 or size > len(longer):
        return False
    for start in range(len(longer) - size + 1):
        if longer[start : start + size] == shorter:
            return True
    return False


def _is_duplicate(candidate: str, previous: str) -> bool:
    """判断是否与上一句重复。

    真实数据里出现过这些需要处理的重复形态：

    * 半句被兜底定稿、随后更完整的版本才出现 → 两者互为前缀；
    * 识别器回溯补标点 / 丢连字符（``trade-off`` → ``trade off``）→
      只有标点差异的应视为同一句；
    * **数字表示被改写**（``one of`` ↔ ``1 of``）→ 见 :func:`_tokens`；
    * **同一段语音被重新识别成不同版本**，例如实测中的
      ``Let us begin with a simple question about scarcity``
      与 ``Begin with a simple question about scarcity``——一方包含另一方。

    .. warning::
       比较必须在**词**的层面做，不能在拼接后的字符串上做字符比较。
       曾经写成 ``a.startswith(b)``，于是候选 ``all of the debian packages…``
       会被上一句 ``A.`` 判成重复（``"all".startswith("a")`` 为真）**整句丢弃**。
       实测（用户 26 分钟日志）这导致 ``All of the Debian packages I installed
       on Ubuntu, but it'll also show you the.`` 一个字都没被翻译。
       ``A.`` ``So.`` ``I.`` 这类碎片一出现，后面同首字母的长句就会被吞掉。
    """
    if not previous:
        return False
    tokens_a = _tokens(candidate)
    tokens_b = _tokens(previous)
    if not tokens_a or not tokens_b:
        return False
    if tokens_a == tokens_b:
        return True
    if _is_token_prefix(tokens_a, tokens_b) or _is_token_prefix(tokens_b, tokens_a):
        return True

    # 包含关系：限制长度，避免把短句误判成重复
    shorter, longer = (
        (tokens_a, tokens_b) if len(tokens_a) <= len(tokens_b) else (tokens_b, tokens_a)
    )
    if len(shorter) >= 4 and _contains_tokens(longer, shorter):
        return True
    return False


@dataclass
class CaptionSegmenter:
    """把渐进式字幕文本流转成「定稿句子流」。

    :param stable_ms: 待定内容多久没变化就兜底定稿。这是**延迟与完整性的主旋钮**：
        实测（真实录制数据回放）1400ms 时平均额外等待 1.60s、最坏 1.96s 且输出干净；
        降到 1200ms 以下会开始出现 ``That trade.`` 这类碎片；升到 1600ms 以上平均
        等待涨到 2.7s、最坏 7.25s。可用 ``tools/measure_latency.py`` 复测。
    :param clause_min_words: 逗号从句门槛，见 :func:`split_complete`。0 表示关闭。
    """

    stable_ms: int = 1800
    dedupe_window: int = 12
    clause_min_words: int = 0
    _pending: str = ""
    _pending_since: float = 0.0
    # 上一轮的最后一行。用于检测实时字幕滚动窗口的「翻页」，见 feed() 的 2a)
    _last_tail: str = ""
    _recent: list[str] = field(default_factory=list)
    # 已经提交过的前导行数。实时字幕的转录只追加、不缩短，所以用行号做进度标记
    # 既能跳过历史、避免重复翻译，也让每次轮询的开销只和「新增行数」相关。
    _processed_lines: int = 0

    # ------------------------------------------------------------------ #
    def _commit(self, sentence: str) -> str | None:
        sentence = strip_noise(sentence)
        if not sentence:
            return None
        # 若这句只是在「已经翻译过的某句」后面又接了新内容，只翻译新增部分。
        # 直接交给下面的重复判定会把它整句当成重复丢掉，**新内容就永久丢失了**。
        # 注意：这里同时覆盖了「实时字幕回溯改写数字/标点」的情况 ——
        # 改写后仍能认出前半段已经翻译过（见 _tokens）。
        for previous in reversed(self._recent):
            remainder = _extension_remainder(sentence, previous)
            if remainder is None:
                continue
            if len(remainder.split()) >= 3:
                sentence = remainder
            else:
                return None
            break
        for previous in reversed(self._recent):
            if _is_duplicate(sentence, previous):
                return None
        self._recent.append(sentence)
        if len(self._recent) > self.dedupe_window:
            del self._recent[0 : len(self._recent) - self.dedupe_window]
        return sentence

    def _commit_line(self, line: str) -> list[str]:
        """提交一整行（该行已定格，不再变化）。"""
        complete, rest = split_complete(line)
        parts = list(complete)
        if rest:
            # 行已定格，尾部残句就是真正的一句
            parts.append(rest)
        if not parts:
            parts = [line]
        emitted: list[str] = []
        for sentence in parts:
            got = self._commit(sentence)
            if got:
                emitted.append(got)
        return emitted

    # ------------------------------------------------------------------ #
    def prime(self, raw: str, now: float | None = None) -> None:
        """把当前已有的转录标记为「已处理」，不产出任何句子。

        为什么必须有这一步
        ------------------
        实时字幕的 ``Name`` 是**整场会话累积**的转录（实测：一次 35 秒的讲话
        就累积了 6 行）。如果程序在会话中途启动，直接 :meth:`feed` 会把历史内容
        全部当成新句子送去翻译——长视频可能是几百句、几百次 API 调用。
        """
        now = time.monotonic() if now is None else now
        lines = [normalize(line) for line in split_lines(raw)]
        lines = [line for line in lines if line]
        if not lines:
            return
        self._processed_lines = max(0, len(lines) - 1)
        self._pending = lines[-1]
        self._pending_since = now
        self._last_tail = lines[-1]
        self._recent.clear()

    # ------------------------------------------------------------------ #
    def feed(self, raw: str, now: float | None = None) -> list[str]:
        """喂入一次字幕原始文本，返回本次新定稿的句子。"""
        now = time.monotonic() if now is None else now
        lines = [normalize(line) for line in split_lines(raw)]
        lines = [line for line in lines if line]
        if not lines:
            return []

        # 转录变短说明实时字幕重启或转录被清空，重新从头跟踪
        if len(lines) <= self._processed_lines:
            self._processed_lines = 0
            self._pending = ""

        finalized: list[str] = []

        # 1) 只处理「新变成非最后一行」的那些行，历史行不再重复遍历
        upper = max(0, len(lines) - 1)
        for line in lines[self._processed_lines : upper]:
            finalized.extend(self._commit_line(line))
        self._processed_lines = max(self._processed_lines, upper)

        # 2) 最后一行仍在生长。判据是「这一句后面还有没有文本」：
        #    有后续文本 → 这一句确实结束了；否则它可能只是「当前识别到的末尾」。
        tail = lines[-1]

        # 2a) 翻页检测 —— 这一步是**防内容丢失**的关键。
        #
        # 实时字幕是**滚动窗口**（实测满 12 行后就开始顶掉最旧的行）。当新的
        # 一行出现时，上一轮的尾行会被顶到倒数第二的位置，而 _processed_lines
        # 早已顶到上限，于是它**再也不会被 _commit_line 处理**；它尚未定稿的
        # 残句就随着 _pending 被新尾行覆盖而**静默消失**。
        #
        # 实测（用户 16 分钟真实日志）：44 行里有 6 行（14%）从头到尾一个字都
        # 没被翻译过，例如 "Air, for most of human history, has been considered
        # a free resource."。原因就是说话人一句接一句没有 1.4s 以上的停顿，
        # 兜底定稿来不及触发，翻页就把内容带走了。
        #
        # 判据用归一化签名而不是原始文本：实时字幕会回溯改写标点/大小写/数字，
        # 逐字比较会把「改写」误判成「翻页」，从而在句中被提前切断。
        if self._last_tail and tail != self._last_tail:
            previous_signature = _signature(self._last_tail)
            moved_up = bool(previous_signature) and any(
                _signature(line).startswith(previous_signature) for line in lines[:-1]
            )
            if moved_up and self._pending and not _is_dangling(self._pending):
                got = self._commit(self._pending)
                if got:
                    finalized.append(got)
                self._pending = ""
                self._pending_since = now
            elif moved_up:
                # 残留只是个功能词（"Of." / "And." / "But."）：那是识别噪声——
                # 实时字幕把句号插在了半个词后面。提交它只会多出一行无意义字幕
                # 和一次浪费的 API 调用，丢掉更划算。
                self._pending = ""
                self._pending_since = now
        self._last_tail = tail

        complete, rest = split_complete(tail, self.clause_min_words)

        if rest:
            safe = complete
            candidate = rest
        else:
            # 除最后一句外都被后续文本证实结束了；
            # 最后一句仍可能是半句，绝不能把整行一起提交（会被去重规则整行吞掉）
            safe = complete[:-1]
            candidate = complete[-1] if complete else tail

        for sentence in safe:
            got = self._commit(sentence)
            if got:
                finalized.append(got)

        # 3) 待定内容兜底：长时间不再变化就认为说完了
        if candidate != self._pending:
            self._pending = candidate
            self._pending_since = now
        elif candidate and (now - self._pending_since) * 1000.0 >= self.stable_ms:
            got = self._commit(candidate)
            if got:
                finalized.append(got)
            self._pending = ""
            self._pending_since = now

        return finalized

    # ------------------------------------------------------------------ #
    def flush(self, now: float | None = None) -> list[str]:
        """强制定稿当前待定内容（退出或暂停时使用）。"""
        if not self._pending:
            return []
        got = self._commit(self._pending)
        self._pending = ""
        self._pending_since = time.monotonic() if now is None else now
        return [got] if got else []

    @property
    def pending(self) -> str:
        """当前尚未定稿的内容，用于在浮窗里先显示原文。"""
        return self._pending

    def reset(self) -> None:
        self._pending = ""
        self._pending_since = 0.0
        self._last_tail = ""
        self._recent.clear()
        self._processed_lines = 0

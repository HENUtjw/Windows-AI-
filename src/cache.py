#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""短语级翻译缓存。

定位：在 ``app.py`` 的 ``_submit`` 之前命中即写 display、不进队列；
在 ``_translator_loop`` 拿到 API 译文之后调 ``observe`` 决定是否晋升。

两道闸门
--------
1. **长度闸门**：归一化后词数 ≤ ``max_words`` 才考虑进缓存（防长句被语境污染）。
2. **稳定性闸门**：同一签名**多数票**达到 ``min_hits`` 票且占比 ≥ ``majority``
   才晋升。用多数票而不是「连续相同」，是因为实测 temperature=1.3 下后者
   几乎不可达（详见 :meth:`TranslationCache.observe`）。

线程安全：所有读写走一个 ``threading.Lock``。
"""

from __future__ import annotations

import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass


@dataclass
class _Candidate:
    """观察中的签名：各译文的得票数 + 最近一次译文。"""
    votes: Counter
    last: str


class TranslationCache:
    """短句翻译的 LRU + 候选观察池。"""

    def __init__(
        self,
        max_words: int = 6,
        min_hits:  int = 3,
        size:      int = 500,
        majority:  float = 0.75,
        min_samples: int = 0,
    ) -> None:
        self.max_words  = max(1, int(max_words))
        self.min_hits   = max(1, int(min_hits))
        self._size_cap  = max(1, int(size))   # 避免与 size() 方法同名
        # 晋升还要求最高票译文占比达到这个比例，否则说明该短语是语境依赖的
        self.majority   = min(1.0, max(0.5, float(majority)))
        # 至少观察这么多次才开始评估晋升（0 表示不额外要求，等同 min_hits）。
        # 少了这道闸门会在「早期走运」时误判：实测 Right. 在第 4 次观察时
        # 恰好凑成 对。3 / 好的。1 = 75%，会带着 50% 的真实分布被晋升。
        # 这里默认不额外要求，由配置项 cache.min_samples 在部署时开启。
        self.min_samples = max(self.min_hits, int(min_samples) or self.min_hits)
        self.enabled: bool = True
        self._candidates: dict[str, _Candidate] = {}
        self._canonical:  OrderedDict[str, str]  = OrderedDict()
        self._lock = threading.Lock()

    # -------------------------------------------------------------- #
    def lookup(self, sig: str) -> str | None:
        """已晋升签名返回译文；未晋升 / 禁用 / 长签名均返回 None。"""
        if not self.enabled:
            return None
        if not sig or len(sig.split()) > self.max_words:
            return None
        with self._lock:
            value = self._canonical.get(sig)
            if value is not None:
                self._canonical.move_to_end(sig)
        return value

    # -------------------------------------------------------------- #
    def observe(self, sig: str, translation: str) -> None:
        """翻译线程拿到 API 响应后调用；观察稳定性，必要时晋升。

        为什么用「多数票」而不是「连续相同」
        ----------------------------------
        实测（temperature=1.3）同一短句重复翻译：

            Yeah.       → 嗯。 ×6            （完全稳定）
            Right.      → 对。×3 好的。×2 嗯。×1（有波动）
            So, let us begin. → 5 次 5 种译法  （几乎随机）

        旧规则要求「同译文**连续** min_hits 次」——在高温采样下这个条件
        几乎不可达：``Right.`` 连续 3 次相同的概率只有百分之十几，``So, let us
        begin.`` 则永远不可能。结果就是缓存**几乎永不命中**。

        改成多数票后：完全稳定的短语照样晋升，而 ``Right.`` 这种最高票只占
        50% 的会被 majority 门槛挡下——正是我们想要的行为（歧义短语不进缓存）。
        """
        if not self.enabled:
            return
        if not sig or not translation:
            return
        if len(sig.split()) > self.max_words:
            return
        with self._lock:
            cand = self._candidates.get(sig)
            if cand is None:
                cand = _Candidate(votes=Counter(), last="")
                self._candidates[sig] = cand
            cand.votes[translation] += 1
            cand.last = translation

            # 晋升判定放在所有分支之外，保证 min_hits=1 时首次观察也能晋升
            if sig not in self._canonical:
                total = sum(cand.votes.values())
                if total >= self.min_samples:
                    top_text, top_votes = cand.votes.most_common(1)[0]
                    if top_votes >= self.min_hits and top_votes / total >= self.majority:
                        self._canonical[sig] = top_text
                        self._canonical.move_to_end(sig)
                        while len(self._canonical) > self._size_cap:
                            self._canonical.popitem(last=False)

            # 候选池软上限：避免长尾签名撑爆内存（已晋升的不动）
            cap = self._size_cap * 4
            if len(self._candidates) > cap:
                for k in list(self._candidates.keys()):
                    if k not in self._canonical:
                        del self._candidates[k]
                        if len(self._candidates) <= self._size_cap * 2:
                            break

    # -------------------------------------------------------------- #
    def set_enabled(self, value: bool) -> None:
        """热更新开关。关闭时清空内存，避免脏命中。"""
        with self._lock:
            self.enabled = bool(value)
            if not self.enabled:
                self._candidates.clear()
                self._canonical.clear()

    # -------------------------------------------------------------- #
    def size(self) -> int:
        """已晋升的 canonical 条数（给诊断与设置面板用）。"""
        with self._lock:
            return len(self._canonical)
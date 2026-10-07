#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内置高频短语包。

定位
----
在学习/翻译链路上位于 **最前面**：命中即用，不调用 API、不消耗并发名额、
没有网络往返——**零延迟**。

质量控制靠「收录原则」而不是靠事后校验
--------------------------------------
本包只收录**脱离上下文也只有一种正确译法**的短语：问候、致谢、回应、
话语标记等。凡是意思随语境变化的（``Right.`` 可能是「对」也可能是「右边」、
``Fine.`` 可能是「好的」也可能是「罚款」）一律不收，交给 AI。

这样就不需要在运行时「用 AI 再验证一遍」——把不确定的东西排除在外，
本身就是质量保证，而且不产生任何额外延迟。

匹配规则
--------
键是**归一化后的整句**（小写、去标点、空格归一，与 ``_spec_key`` 同一套），
必须与整条字幕**完全相等**才命中。因此：:

    Yeah.                    → 命中（整条字幕就是它）
    Yeah, I think so.        → 不命中，整句交给 AI

这点很关键：短语包不会把 ``Yeah`` 的译文塞进一个更长的句子里。

线程安全：加载后是只读字典，多线程查找天然安全。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "phrasebook.json"

_WHITESPACE = re.compile(r"\s+")


def _key(text: str) -> str:
    """与 ``app._spec_key`` 保持同一套归一化：折叠空白 + 小写。"""
    return _WHITESPACE.sub(" ", str(text)).casefold().strip()


class Phrasebook:
    """只读的短语包。键为归一化整句。"""

    def __init__(self, entries: dict[str, str] | None = None, source: Path | None = None) -> None:
        self._entries: dict[str, str] = dict(entries or {})
        self.source = source

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Path | str | None = None) -> "Phrasebook":
        """读取短语包。文件缺失或损坏时返回空包（不影响正常翻译）。"""
        target = Path(path) if path else DEFAULT_PATH
        try:
            raw = json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return cls({}, target)
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            return cls({}, target)
        clean = {
            _key(key): str(value).strip()
            for key, value in entries.items()
            if str(key).strip() and str(value).strip()
        }
        return cls(clean, target)

    # ------------------------------------------------------------------ #
    def lookup(self, sig: str) -> str | None:
        """命中返回译文，否则返回 None。"""
        if not sig:
            return None
        return self._entries.get(_key(sig))

    def size(self) -> int:
        return len(self._entries)

    def __contains__(self, sig: object) -> bool:
        return isinstance(sig, str) and _key(sig) in self._entries

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内置短语包与翻译链路第一层的测试（不需要网络）。

运行::

    python tests/test_phrasebook.py

退出码 0 表示全部通过。
"""

from __future__ import annotations

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from src.app import _spec_key  # noqa: E402
from src.phrasebook import Phrasebook  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}   {detail}")
        FAILURES.append(name)


# ---------------------------------------------------------------- #
def test_builtin_pack_loads() -> None:
    """内置短语包能加载，且条数合理。"""
    book = Phrasebook.load()
    check("pack_not_empty", book.size() >= 50, str(book.size()))
    check("pack_source_exists", book.source is not None and book.source.exists(), str(book.source))


def test_lookup_normalizes_case_and_punctuation() -> None:
    """签名归一化后命中：大小写、标点、撇号都不影响。"""
    book = Phrasebook.load()
    for text in ["Thank you.", "thank you", "THANK YOU!", "Thank  you."]:
        check(f"hit {text!r}", book.lookup(_spec_key(text)) == "谢谢。")
    check("apostrophe_stripped", book.lookup(_spec_key("You're welcome.")) == "不客气。")


def test_whole_sentence_match_only() -> None:
    """必须整句相等才命中 —— 不能把短语译文塞进更长的句子里。

    这是质量保证的关键：``Yeah`` 有译文，但 ``Yeah, I think so.`` 必须整句
    交给 AI，否则会拼出错误的句子。
    """
    book = Phrasebook.load()
    check("standalone_hits", book.lookup(_spec_key("Yeah.")) == "嗯。")
    check("longer_sentence_misses", book.lookup(_spec_key("Yeah, I think so.")) is None)
    check("longer_sentence_misses_2", book.lookup(_spec_key("Right, that is the point.")) is None)


def test_ambiguous_phrases_excluded() -> None:
    """收录原则：意思随语境变化的一律不收（收进来就必须靠 AI 复核，得不偿失）。"""
    book = Phrasebook.load()
    for text in [
        "Fine.",           # 「好的」还是「罚款」
        "No way.",         # 「不行」还是「没门」
        "You are right.",  # 「你是对的」还是「你在右边」
        "It is on me.",    # 「我请客」还是「在我身上」
    ]:
        check(f"excluded {text!r}", book.lookup(_spec_key(text)) is None)


def test_missing_or_broken_file_is_safe() -> None:
    """文件缺失或损坏时返回空包，绝不能影响正常翻译。"""
    missing = Phrasebook.load(os.path.join(_ROOT, "data", "_not_exist_.json"))
    check("missing_file_empty", missing.size() == 0)
    check("missing_file_miss", missing.lookup("yeah") is None)

    broken_path = os.path.join(_ROOT, "_broken_phrasebook.json")
    with open(broken_path, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")
    try:
        broken = Phrasebook.load(broken_path)
        check("broken_file_empty", broken.size() == 0)
    finally:
        os.remove(broken_path)


def test_custom_pack_supported() -> None:
    """用户可以用自己的文件扩充短语包（提高命中率最直接的办法）。"""
    path = os.path.join(_ROOT, "_custom_phrasebook.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"entries": {"opportunity cost": "机会成本", "  Marginal Utility ": "边际效用"}},
                  handle, ensure_ascii=False)
    try:
        book = Phrasebook.load(path)
        check("custom_size", book.size() == 2, str(book.size()))
        check("custom_lookup", book.lookup(_spec_key("Opportunity cost.")) == "机会成本")
        check("custom_key_trimmed", book.lookup("marginal utility") == "边际效用")
    finally:
        os.remove(path)


def test_empty_sig_is_safe() -> None:
    book = Phrasebook.load()
    check("empty_sig_none", book.lookup("") is None)
    check("contains_operator", ("yeah" in book) and ("nope_not_a_phrase" not in book))


# ---------------------------------------------------------------- #
def main() -> int:
    test_builtin_pack_loads()
    test_lookup_normalizes_case_and_punctuation()
    test_whole_sentence_match_only()
    test_ambiguous_phrases_excluded()
    test_missing_or_broken_file_is_safe()
    test_custom_pack_supported()
    test_empty_sig_is_safe()
    print()
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("全部通过 (ALL PASS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

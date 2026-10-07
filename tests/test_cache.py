#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TranslationCache 单元测试。

测试目标（见 docs/superpowers/specs/2026-10-07-translation-cache-design.md）:
  - 两道闸门：长度 ≤ max_words；同签名连续相同译文 ≥ min_hits 才晋升。
  - LRU 容量控制。
  - 线程安全。
  - 禁用即清空。
"""

from __future__ import annotations

import os
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from src.cache import TranslationCache  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}   {detail}")
        FAILURES.append(name)


def make_cache(**overrides) -> TranslationCache:
    defaults = dict(max_words=6, min_hits=3, size=500)
    defaults.update(overrides)
    return TranslationCache(**defaults)


# ---------------------------------------------------------------- #
def test_lookup_empty_returns_none() -> None:
    """空 cache 永远 miss。"""
    cache = make_cache()
    check("empty_lookup", cache.lookup("anything") is None)
    check("empty_size", cache.size() == 0)


def test_promote_after_min_hits() -> None:
    """同 sig 同译文 3 次 → 第 4 次命中；前 3 次仍 miss。"""
    cache = make_cache(min_hits=3)
    sig = "yeah"
    check("hit_1_miss", cache.lookup(sig) is None)
    cache.observe(sig, "是的")
    check("hit_2_miss", cache.lookup(sig) is None)
    cache.observe(sig, "是的")
    check("hit_3_miss", cache.lookup(sig) is None)
    cache.observe(sig, "是的")
    check("hit_4_returned", cache.lookup(sig) == "是的")
    check("hit_4_size_one", cache.size() == 1)


def test_promote_reset_on_different_translation() -> None:
    """同 sig 不同译文 → 计数重置，不晋升。"""
    cache = make_cache(min_hits=3)
    sig = "right"
    cache.observe(sig, "是的")
    cache.observe(sig, "是的")
    cache.observe(sig, "向右转")        # 不同 → 重置
    check("reset_miss", cache.lookup(sig) is None)
    cache.observe(sig, "向右转")
    cache.observe(sig, "向右转")
    check("still_not_promoted", cache.lookup(sig) is None)


def test_long_text_never_cached() -> None:
    """> max_words 词的 sig 永不进 canonical。"""
    cache = make_cache(max_words=3, min_hits=2)
    sig = "when you watch this video you could be doing something else"
    for _ in range(10):
        cache.observe(sig, "译文")
    check("long_text_miss", cache.lookup(sig) is None)
    check("long_text_zero_size", cache.size() == 0)


def test_lru_eviction() -> None:
    """超 size 后最旧 canonical 被踢出。"""
    cache = make_cache(min_hits=1, size=3)
    cache.observe("a", "甲")
    cache.observe("b", "乙")
    cache.observe("c", "丙")
    cache.observe("d", "丁")           # 现在已超 3，下一次 observe 触发驱逐
    check("lru_evicted_a", cache.lookup("a") is None)
    check("lru_kept_b",   cache.lookup("b") == "乙")


def test_lookup_eviction_race() -> None:
    """lookup 与 observe 触发驱逐并发时，不抛 KeyError。"""
    cache = make_cache(min_hits=1, size=10)
    for i in range(10):
        cache.observe(f"k{i}", str(i))

    errors: list[BaseException] = []

    def do_lookup() -> None:
        try:
            for _ in range(1000):
                cache.lookup("k0")
        except BaseException as exc:                    # noqa: BLE001
            errors.append(exc)

    def do_observe() -> None:
        try:
            for _ in range(1000):
                cache.observe("zz", "v")
        except BaseException:
            pass

    t1 = threading.Thread(target=do_lookup)
    t2 = threading.Thread(target=do_observe)
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    check("lookup_no_keyerror_under_eviction", not errors, str(errors[:1]))


def test_disable_clears_cache() -> None:
    """set_enabled(False) 后内存清空，lookup 全 miss。"""
    cache = make_cache(min_hits=1)
    cache.observe("hi", "你好")
    check("pre_disable_hit", cache.lookup("hi") == "你好")
    cache.set_enabled(False)
    check("disabled_miss", cache.lookup("hi") is None)
    check("disabled_size", cache.size() == 0)
    cache.set_enabled(True)
    cache.observe("hi", "你好")
    cache.observe("hi", "你好")
    check("reenabled_hit", cache.lookup("hi") == "你好")


def test_thread_safety() -> None:
    """多线程并发 observe/lookup 不崩、不抛异常。"""
    cache = make_cache(min_hits=100)   # 高门槛保证大部分 observe 不晋升

    errors: list[BaseException] = []

    def worker(thread_id: int) -> None:
        try:
            for i in range(100):
                sig = f"sig_{thread_id}_{i % 5}"
                cache.observe(sig, f"译文_{i}")
                _ = cache.lookup(sig)
        except BaseException as exc:                    # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    check("no_exceptions", not errors, str(errors[:1]))


def test_majority_blocks_ambiguous_phrase() -> None:
    """语境依赖短语不得晋升 —— 这正是把「连续相同」改成「多数票」的目的。

    实测（temperature=1.3）``Right.`` 的译文分布是 对。×3 / 好的。×2 / 嗯。×1，
    最高票只占 50%。这种短语若进缓存，遇到「右边」的语境就会给出错误译文。

    用 min_samples=5 是实际部署的配置（cache.min_samples）。
    """
    cache = make_cache(min_hits=3, majority=0.75, min_samples=5)
    for text in ["对。", "对。", "好的。", "对。", "好的。", "嗯。"]:
        cache.observe("right", text)
    check("ambiguous_not_promoted", cache.lookup("right") is None, str(cache.size()))


def test_early_lucky_streak_does_not_promote() -> None:
    """回归：早期走运不能晋升。

    实测顺序下，``Right.`` 在第 4 次观察时恰好是 对。3 / 好的。1 = 75%，
    只看比例会误判为「稳定」。min_samples 闸门就是挡这个的。
    """
    cache = make_cache(min_hits=3, majority=0.75, min_samples=5)
    for text in ["对。", "对。", "好的。", "对。"]:
        cache.observe("right", text)
    check("4th_observation_not_promoted", cache.lookup("right") is None, "3/4=75%，但样本不足")
    cache.observe("right", "好的。")   # 5 次：对。3 / 好的。2 = 60%
    check("5th_observation_not_promoted", cache.lookup("right") is None, "3/5=60%")


def test_majority_promotes_stable_phrase() -> None:
    """完全稳定的短语必须能晋升 —— 否则缓存永远不命中，等于没做。"""
    cache = make_cache(min_hits=3, majority=0.75, min_samples=5)
    for _ in range(5):
        cache.observe("yeah", "嗯。")
    check("stable_promoted", cache.lookup("yeah") == "嗯。")


def test_majority_tolerates_outlier() -> None:
    """4 票相同 + 1 票不同 = 80% ≥ 75%，应当晋升。"""
    cache = make_cache(min_hits=3, majority=0.75, min_samples=5)
    cache.observe("okay", "好的。")
    cache.observe("okay", "好的。")
    cache.observe("okay", "行。")
    cache.observe("okay", "好的。")
    cache.observe("okay", "好的。")
    check("outlier_tolerated", cache.lookup("okay") == "好的。")


def test_promotion_does_not_need_consecutive() -> None:
    """回归测试：票数**不需要连续**。

    旧规则要求「同译文连续 min_hits 次」，在 temperature=1.3 下几乎不可达
    （``Right.`` 连续 3 次相同的概率只有百分之十几）——这正是「缓存无法命中」
    的根因。改成多数票后，只要最高票占比够，间隔着出现也能晋升。
    """
    cache = make_cache(min_hits=3, majority=0.75, min_samples=5)
    for text in ["对。", "别的", "对。", "别的", "对。"]:
        cache.observe("right", text)
    check("below_majority_not_promoted", cache.lookup("right") is None, "3/5=60%")
    cache.observe("right", "对。")
    check("still_below_majority", cache.lookup("right") is None, "4/6=67%")
    cache.observe("right", "对。")     # 5/7 = 71%
    cache.observe("right", "对。")     # 6/8 = 75% → 达标
    check("eventually_promoted", cache.lookup("right") == "对。")


# ---------------------------------------------------------------- #
def main() -> int:
    test_lookup_empty_returns_none()
    test_promote_after_min_hits()
    test_promote_reset_on_different_translation()
    test_long_text_never_cached()
    test_lru_eviction()
    test_lookup_eviction_race()
    test_disable_clears_cache()
    test_thread_safety()
    test_majority_blocks_ambiguous_phrase()
    test_early_lucky_streak_does_not_promote()
    test_majority_promotes_stable_phrase()
    test_majority_tolerates_outlier()
    test_promotion_does_not_need_consecutive()
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
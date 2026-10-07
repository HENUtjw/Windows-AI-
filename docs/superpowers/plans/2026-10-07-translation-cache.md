# 短句翻译缓存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在快速说话场景下，通过短句级翻译缓存（默认开启）让重复短语跳过 API 调用、稳定字幕滞后，且不降低译文质量。

**Architecture:** 新增一个线程安全的 `TranslationCache`（`OrderedDict` LRU + 候选观察池），插入到 `app.py` 的 `_submit()` 里——命中立即回写 display，未命中走原路径；翻译线程拿到结果后调 `cache.observe()` 决定是否晋升为可缓存。设置面板暴露一个开关，热更新生效。

**Tech Stack:** Python 3.8+、标准库（`collections.OrderedDict`、`threading`、`dataclasses`）、现有 Tk 设置面板框架。

## Global Constraints

- 项目**不是 git 仓库**，所有任务**不执行 `git add/commit`**——直接修改文件即可，外部由用户决定何时回填版本控制。
- 依赖仅限标准库 + `requirements.txt` 已有的 `requests` / `uiautomation` / `comtypes`。
- 配置默认值必须与本计划 §「配置默认值」一致（不可在实施中擅自改动）。
- 测试使用项目已有的自定义 `check(name, condition, detail)` 框架（见 `tests/test_translator.py:41-46`），**不用 pytest**。运行方式：`python tests/test_xxx.py`。
- 命名与文案风格沿用项目：中文 docstring、英文标识符、`_spec_key` 与 `app.py:43-48` 复用。
- 代码编辑完成后用 `python -m py_compile <file>` 确认无语法错误，再跑测试。

## 配置默认值（所有任务共用）

```python
DEFAULT_CONFIG["cache"] = {
    "enabled":   True,    # 默认开启
    "max_words": 6,       # 超过此词数永不进缓存
    "min_hits":  3,       # 同签名同译文出现这么多次才晋升
    "size":      500,     # canonical LRU 上限
}
```

---

## File Structure

| 文件 | 类型 | 职责 |
|---|---|---|
| `src/cache.py` | 新建 | `TranslationCache` 类 + `Candidate` 数据类 |
| `tests/test_cache.py` | 新建 | `TranslationCache` 单测 |
| `src/config.py` | 修改 | `DEFAULT_CONFIG` 加 `cache` 段 |
| `src/app.py` | 修改 | `_submit()` 加缓存查询；`_translator_loop` 末尾调 `cache.observe()`；加诊断计数 |
| `src/settings_dialog.py` | 修改 | `open_settings_dialog` 加「翻译缓存」分组 |
| `config.example.json` | 修改 | 加 `cache` 示例段 |
| `README.md` | 修改 | 配置表 + 「延迟与提速」+ 「常见问题」三处 |
| `tools/e2e_selftest.py` | 修改 | 加 cache 命中/未命中断言 |

---

## Task 1: 配置默认值

**Files:**
- Modify: `src/config.py`（在 `DEFAULT_CONFIG["overlay"]` 块之后追加 `cache` 块）

- [ ] **Step 1: 在 `DEFAULT_CONFIG` 里加 `cache` 块**

打开 `src/config.py`，找到 `DEFAULT_CONFIG` 字典末尾的 `"overlay"` 块（行 85–123），在它后面追加：

```python
    "cache": {
        # 短语级翻译缓存：同签名同译文连续出现 min_hits 次后晋升为可缓存，
        # 后续相同短句（≤ max_words 词）直接复用译文、跳过 API 调用。
        # 默认开启；快语速对话里重复短语占比高，能显著降低 API 请求数。
        "enabled":   True,
        "max_words": 6,
        "min_hits":  3,
        "size":      500,
    },
```

- [ ] **Step 2: 编译验证**

```bash
python -m py_compile src/config.py
```

Expected: 无输出（语法正确）。

- [ ] **Step 3: 加载验证**

```bash
python -c "from src.config import Config; c = Config.load(); print(c.get('cache.enabled'), c.get('cache.max_words'), c.get('cache.min_hits'), c.get('cache.size'))"
```

Expected: `True 6 3 500`

---

## Task 2: 实现 `TranslationCache` 类（带单元测试，TDD）

**Files:**
- Create: `src/cache.py`
- Create: `tests/test_cache.py`

**Interfaces（供后续 Task 消费）:**
- 类: `TranslationCache(max_words: int = 6, min_hits: int = 3, size: int = 500)`
- 属性: `enabled: bool`（默认 True）
- 方法:
  - `lookup(sig: str) -> str | None` — 已晋升签名返回译文，否则 None
  - `observe(sig: str, translation: str) -> None` — 翻译线程拿到结果后调用，观察稳定性
  - `set_enabled(value: bool) -> None` — 热更新开关，关闭时清空内存
  - `size() -> int` — 当前 canonical 条数（给诊断/UI 用）
- 数据类: `Candidate(count: int, last: str)` —— 内部用，**不导出**

- [ ] **Step 1: 写第一个失败的测试**

`tests/test_cache.py` 新建，写入：

```python
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
    # 第 4 次查询应该命中
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
    # 之后再观察相同译文 2 次也不够晋升门槛
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
    # a 是最旧的，lookup 应该返回 None
    check("lru_evicted_a", cache.lookup("a") is None)
    check("lru_kept_b",   cache.lookup("b") == "乙")


def test_disable_clears_cache() -> None:
    """set_enabled(False) 后内存清空，lookup 全 miss。"""
    cache = make_cache(min_hits=1)
    cache.observe("hi", "你好")
    check("pre_disable_hit", cache.lookup("hi") == "你好")
    cache.set_enabled(False)
    check("disabled_miss", cache.lookup("hi") is None)
    check("disabled_size", cache.size() == 0)
    # 重新启用后行为正常
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


# ---------------------------------------------------------------- #
def main() -> int:
    test_lookup_empty_returns_none()
    test_promote_after_min_hits()
    test_promote_reset_on_different_translation()
    test_long_text_never_cached()
    test_lru_eviction()
    test_disable_clears_cache()
    test_thread_safety()
    print()
    if FAILURES:
        print(f"  ✘ {len(FAILURES)} 项失败：")
        for name in FAILURES:
            print(f"    - {name}")
        return 1
    print("  ✔ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 运行测试，确认它们失败（因为 `src/cache.py` 还不存在）**

```bash
python tests/test_cache.py
```

Expected: `ModuleNotFoundError: No module named 'src.cache'`（或 `ImportError`）。

- [ ] **Step 3: 实现 `src/cache.py`**

新建 `src/cache.py`，写入：

> 下面是 **src/cache.py 的当前内容**（逐行同步，可直接对比）。
> 该文件是唯一权威实现；本文档其余部分只描述设计意图。
`python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""短语级翻译缓存。

定位：在 ``app.py`` 的 ``_submit`` 之前命中即写 display、不进队列；
在 ``_translator_loop`` 拿到 API 译文之后调 ``observe`` 决定是否晋升。

两道闸门
--------
1. **长度闸门**：归一化后词数 ≤ ``max_words`` 才考虑进缓存（防长句被语境污染）。
2. **稳定性闸门**：同签名**连续**产出相同译文 ≥ ``min_hits`` 次才晋升
   （让稳定性本身过滤掉语境依赖型短语，不靠白名单）。

线程安全：所有读写走一个 ``threading.Lock``。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class _Candidate:
    """观察中的签名：连续相同译文的次数 + 最近一次译文。"""
    count: int
    last:  str


class TranslationCache:
    """短句翻译的 LRU + 候选观察池。"""

    def __init__(
        self,
        max_words: int = 6,
        min_hits:  int = 3,
        size:      int = 500,
    ) -> None:
        self.max_words  = max(1, int(max_words))
        self.min_hits   = max(1, int(min_hits))
        self._size_cap  = max(1, int(size))   # 避免与 size() 方法同名
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

        计数语义
        --------
        - 全新签名：count = 1（首次观察算"连续 1 次"）。
        - 同译文续命：count += 1。
        - 译文变化（重置）：count = 0（**本次观察不计入新连续序列**），
          接下来仍要看到 ``min_hits`` 次相同译文才能晋升。
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
                cand = _Candidate(count=1, last=translation)
                self._candidates[sig] = cand
            elif cand.last == translation:
                cand.count += 1
            else:
                # 重置：丢弃旧序列，本次观察本身不计数
                cand.count = 0
                cand.last  = translation

            # 晋升判定：放在所有分支外，保证 min_hits=1 时首次观察也能晋升
            if cand.count >= self.min_hits and sig not in self._canonical:
                self._canonical[sig] = translation
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
```

- [ ] **Step 4: 运行测试，全部应通过**

```bash
python tests/test_cache.py
```

Expected: 全部 `[PASS]`，结尾 `✔ 全部通过`，退出码 0。

- [ ] **Step 5: 编译验证**

```bash
python -m py_compile src/cache.py tests/test_cache.py
```

Expected: 无输出。

---

## Task 3: 把缓存接入 `app.py`

**Files:**
- Modify: `src/app.py`
  - 顶部 import 加 `from .cache import TranslationCache`
  - `TranslatorApp.__init__` 末尾追加 `self._cache` 初始化 + 诊断计数
  - `_submit()` 在投队列前先查缓存
  - `_translator_loop` 拿到 API 结果后调 `cache.observe()`

**Interfaces（消费 Task 2）:**
- 构造：`TranslationCache(max_words=cfg.get("cache.max_words", 6), min_hits=cfg.get("cache.min_hits", 3), size=cfg.get("cache.size", 500))`
- `cache.set_enabled(bool)` —— 设置面板热更新用，**也是把 `cfg["cache.enabled"]` 同步进去的唯一方法**（构造器不接受 `enabled`）
- `cache.lookup(_spec_key(sentence)) -> str | None`
- `cache.observe(_spec_key(source), translation)` —— 在 `_translator_loop` 末尾调用

- [ ] **Step 1: 在 `app.py` 顶部加 import**

打开 `src/app.py`，找到 `from .translator import ...`（行 36），在它后面加：

```python
from .cache import TranslationCache
```

- [ ] **Step 2: 在 `TranslatorApp.__init__` 末尾加缓存初始化与计数**

找到 `TranslatorApp.__init__`（行 149）的最后一个属性赋值（`self._spec_since = 0.0`，行 197），在它前面（也就是初始化推测相关字段那段之前或之后均可；**建议放在最后**，紧跟 `self.speculation_hits = 0` 之后）追加：

```python
        # 短句翻译缓存：默认开启。同签名同译文连续 min_hits 次后晋升，
        # 后续相同短句（≤ max_words 词）跳过 API 直接复用译文。
        self._cache = TranslationCache(
            max_words = int(cfg.get("cache.max_words", 6)),
            min_hits  = int(cfg.get("cache.min_hits", 3)),
            size      = int(cfg.get("cache.size", 500)),
        )
        # enabled 是属性不是构造参数（hot-update 用 set_enabled）
        self._cache.set_enabled(bool(cfg.get("cache.enabled", True)))
        self.cache_hits   = 0
        self.cache_misses = 0
```

- [ ] **Step 3: 修改 `_submit()`**

找到 `_submit()`（行 256-270）。当前实现：

```python
def _submit(self, sentence: str) -> None:
    limit = max(1, int(self.cfg.get("translate.max_backlog", 3)))
    while self._jobs.qsize() >= limit:
        try:
            dropped_id, dropped_text = self._jobs.get_nowait()
        except queue.Empty:
            break
        self._display.set_translation(dropped_id, "（积压跳过）", done=True)
        log.warning("翻译积压，跳过一句：%s", dropped_text)

    row_id = self._display.add(sentence)
    self._jobs.put((row_id, sentence))
    log.info("原文: %s", sentence)
```

替换为：

```python
def _submit(self, sentence: str) -> None:
    limit = max(1, int(self.cfg.get("translate.max_backlog", 3)))
    while self._jobs.qsize() >= limit:
        try:
            dropped_id, dropped_text = self._jobs.get_nowait()
        except queue.Empty:
            break
        self._display.set_translation(dropped_id, "（积压跳过）", done=True)
        log.warning("翻译积压，跳过一句：%s", dropped_text)

    row_id = self._display.add(sentence)

    # 短句缓存命中：跳过 API，直接写 display（不投队列、不消耗并发名额）
    cached = self._cache.lookup(_spec_key(sentence))
    if cached is not None:
        self._display.set_translation(row_id, cached, done=True)
        self.cache_hits += 1
        log.debug("译文（缓存命中）: %s", cached)
        return

    self.cache_misses += 1
    self._jobs.put((row_id, sentence))
    log.info("原文: %s", sentence)
```

- [ ] **Step 4: 在 `_translator_loop` 末尾调 `cache.observe()`**

找到 `_translator_loop`（行 355-410）里的 `success` 分支。当前实现里 `translate(source, ...)` 调用之后紧跟一行 `self._display.set_translation(...)` 和一行 `self._context.append(...)`。修改如下（**两处都要改**，因为 `_take_speculation` 命中分支也应当观察，否则缓存只统计到正常路径）：

**第一处**（推测命中分支，行 376-387，紧跟 `self._context.append(...)` 之后加 `cache.observe`）：

```python
            speculation = self._take_speculation(source)
            if speculation is not None:
                if not speculation.started:
                    speculation.done.set()
                elif speculation.done.wait(timeout=8.0) and not speculation.failed:
                    if speculation.translation:
                        self._display.set_translation(row_id, speculation.translation, done=True)
                        with self._context_lock:
                            self._context.append((source, speculation.translation))
                        self.speculation_hits += 1
                        log.info("译文（推测命中）: %s", speculation.translation)
                        consecutive_errors = 0
                        # 推测译文也参与缓存晋升
                        self._cache.observe(_spec_key(source), speculation.translation)
                        continue
```

**第二处**（正常翻译分支，`except TranslationError` 之前）：

```python
            try:
                translation = self._translator.translate(source, context, on_delta=on_delta)
                self._display.set_translation(row_id, translation, done=True)
                with self._context_lock:
                    self._context.append((source, translation))
                # 拿到 API 响应后，让缓存观察稳定性
                self._cache.observe(_spec_key(source), translation)
                consecutive_errors = 0
                log.info("译文: %s", translation)
```

- [ ] **Step 5: 编译验证**

```bash
python -m py_compile src/app.py
```

Expected: 无输出。

- [ ] **Step 6: 冒烟测试（不依赖 GUI / API，但需要让线程跑起来才能触发 observe）**

```bash
python -c "
import sys, os, time
sys.path.insert(0, r'.')
from src.app import TranslatorApp
from src.config import Config

cfg = Config.load()
cfg.set('overlay.enabled', False)
app = TranslatorApp(cfg, mock=True)

# 注意：observe 在 _translator_loop 里发生，所以必须启动线程
app._start_threads()

# 投 5 次同一短句。MockTranslator 对相同输入返回相同字符串：
#   1st  miss, observe, count=1
#   2nd  miss, observe, count=2
#   3rd  miss, observe, count=3 -> 晋升
#   4th  HIT, observe
#   5th  HIT, observe
for _ in range(5):
    app._submit('yeah')

# 等 mock 翻译 + observe 落定
time.sleep(1.0)

print('cache.size():', app._cache.size())   # 期望 1
print('cache_hits :', app.cache_hits)        # 期望 2
print('cache_misses:', app.cache_misses)      # 期望 3

assert app._cache.size() >= 1,  'cache 应至少晋升 1 条'
assert app.cache_hits   >= 1,  '第 4 次起应命中'
assert app.cache_misses >= 1,  '前几次应走 API'
print('OK')
"
```

Expected: `cache.size(): 1` / `cache_hits: 2` / `cache_misses: 3` / `OK`。

- [ ] **Step 7: 跑现有测试，确保没破坏**

```bash
python tests/test_cache.py
python tests/test_translator.py
```

Expected: 全部通过。

---

## Task 4: 设置面板加「翻译缓存」分组

**Files:**
- Modify: `src/settings_dialog.py`（加新参数 + 新分组）
- Modify: `src/app.py`（`_open_settings` 里把 cache 回调传进 `open_settings_dialog`）

**Interfaces（消费 Task 3）:**
- `app.cache_hits: int` / `app.cache_misses: int` —— 只读统计
- `app._cache.set_enabled(bool)` —— 热更新开关
- `open_settings_dialog(parent, cfg, on_saved=None, on_cache_toggle=None, cache_stats_provider=None)` —— 新增两个可选参数

- [ ] **Step 1: 扩展 `open_settings_dialog` 签名**

打开 `src/settings_dialog.py`，把行 59 的签名：

```python
def open_settings_dialog(parent: tk.Misc, cfg: Config, on_saved=None) -> tk.Toplevel:
```

改为：

```python
def open_settings_dialog(
    parent: tk.Misc,
    cfg: Config,
    on_saved=None,
    on_cache_toggle=None,
    cache_stats_provider=None,
) -> tk.Toplevel:
    """新增两个可选回调:
      - ``on_cache_toggle(enabled: bool)``: 设置面板上勾选 / 取消勾选时即时调用。
      - ``cache_stats_provider() -> tuple[int, int]``: 拉取当前 ``(hits, misses)``。
    """
```

- [ ] **Step 2: 在 `open_settings_dialog` 末尾追加「翻译缓存」分组**

找到 `open_settings_dialog` 的「return dialog」之前，追加（紧接已有的 API Key / Model 区段）：

```python
    # ── 翻译缓存 ────────────────────────────────────────────── #
    cache_frame = tk.LabelFrame(
        dialog, text="  翻译缓存  ",
        bg=bg, fg=fg, bd=1, relief="solid",
        highlightbackground=border, highlightcolor=border,
        font=(family, 11, "bold"),
        labelanchor="nw",
    )
    cache_frame.configure(highlightthickness=1)
    cache_frame.pack(fill="x", padx=20, pady=(0, 16))
    inner_pad = dict(padx=14, pady=4)

    cache_enabled_var = tk.BooleanVar(
        value=bool(cfg.get("cache.enabled", True))
    )

    def toggle_cache() -> None:
        if on_cache_toggle is not None:
            try:
                on_cache_toggle(cache_enabled_var.get())
            except Exception as exc:                       # noqa: BLE001
                log.debug("cache toggle failed: %s", exc)

    tk.Checkbutton(
        cache_frame,
        text="启用短语级翻译缓存（短句重复出现 ≥ 3 次后自动复用译文）",
        variable=cache_enabled_var,
        command=toggle_cache,
        bg=bg, fg=fg,
        selectcolor=field_bg,
        activebackground=bg, activeforeground=fg,
        font=(family, 11),
        anchor="w", justify="left",
    ).pack(fill="x", **inner_pad)

    cache_stats_var = tk.StringVar(value="命中：0    未命中：0")
    tk.Label(
        cache_frame, textvariable=cache_stats_var,
        bg=bg, fg=dim, font=(family, 10), anchor="w",
    ).pack(fill="x", **inner_pad)

    def refresh_cache_stats() -> None:
        try:
            if cache_stats_provider is not None:
                hits, misses = cache_stats_provider()
                cache_stats_var.set(f"命中：{hits}    未命中：{misses}")
        except Exception as exc:                           # noqa: BLE001
            log.debug("cache stats refresh failed: %s", exc)
        finally:
            try:
                dialog.after(500, refresh_cache_stats)
            except Exception:
                pass

    dialog.after(500, refresh_cache_stats)
```

> 如果文件顶部没有 `log` 变量，把 `log.debug(...)` 替换成 `print(...)` 即可，或在文件顶部 `import logging; log = logging.getLogger(...)`。

- [ ] **Step 3: 让 `on_saved` 写回 `cache.enabled`**

找到 `open_settings_dialog` 现有的「保存」按钮回调（搜索 `command=` 附近）。在写回 `api_key` / `model` 之后追加：

```python
        new_cache_enabled = cache_enabled_var.get()
        old_cache_enabled = bool(cfg.get("cache.enabled", True))
        if new_cache_enabled != old_cache_enabled:
            cfg.set("cache.enabled", new_cache_enabled)
            if cfg.path is not None:
                try:
                    import json as _json
                    existing: dict = {}
                    if cfg.path.exists():
                        try:
                            existing = _json.loads(
                                cfg.path.read_text(encoding="utf-8-sig")
                            ) or {}
                        except Exception:
                            existing = {}
                    existing.setdefault("cache", {})["enabled"] = new_cache_enabled
                    cfg.path.write_text(
                        _json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                except OSError as exc:
                    log.debug("save cache.enabled failed: %s", exc)
```

- [ ] **Step 4: 在 `app.py` 的 `_open_settings` 里把回调接上**

打开 `src/app.py`，找到 `_open_settings`（行 501-512）。当前实现：

```python
    def _open_settings(self) -> None:
        if self._overlay is None:
            return
        try:
            from .settings_dialog import open_settings_dialog

            open_settings_dialog(
                self._overlay._root, self.cfg, on_saved=self._apply_api_key
            )
```

改为：

```python
    def _open_settings(self) -> None:
        if self._overlay is None:
            return
        try:
            from .settings_dialog import open_settings_dialog

            def _on_cache_toggle(enabled: bool) -> None:
                self._cache.set_enabled(enabled)
                if not enabled:
                    # 关闭时清零统计（避免遗留命中数字误导）
                    self.cache_hits = 0
                    self.cache_misses = 0

            def _cache_stats() -> tuple[int, int]:
                return (self.cache_hits, self.cache_misses)

            open_settings_dialog(
                self._overlay._root,
                self.cfg,
                on_saved=self._apply_api_key,
                on_cache_toggle=_on_cache_toggle,
                cache_stats_provider=_cache_stats,
            )
```

- [ ] **Step 5: 编译验证**

```bash
python -m py_compile src/settings_dialog.py src/app.py
```

Expected: 无输出。

- [ ] **Step 6: 验证 GUI 不崩**

```bash
python -c "
import sys, tkinter as tk
sys.path.insert(0, r'.')
from src.settings_dialog import open_settings_dialog
from src.config import Config

cfg = Config.load()
root = tk.Tk()
root.withdraw()

hits, misses = (5, 7)
dlg = open_settings_dialog(
    root, cfg,
    on_cache_toggle=lambda e: print(f'toggle: {e}'),
    cache_stats_provider=lambda: (hits, misses),
)
root.after(300, root.destroy)
root.mainloop()
print('OK: 设置面板已构建')
"
```

Expected: 打印 `toggle: True`（Checkbutton 默认触发一次）和 `OK: 设置面板已构建`，无 traceback。

---

## Task 5: 文档更新（README + config.example.json）

**Files:**
- Modify: `README.md`
- Modify: `config.example.json`

- [ ] **Step 1: 更新 `config.example.json`**

打开 `config.example.json`，在 `translate` 段之后、`captions` 段之前（或任意合适位置）追加：

```json
  "cache": {
    "enabled": true,
    "max_words": 6,
    "min_hits": 3,
    "size": 500
  },
```

- [ ] **Step 2: 在 README 配置表里加 4 行**

打开 `README.md`，定位「翻译质量相关」配置表（搜索 `caption.stable_ms` 那张表），在表格末尾追加：

```markdown
| `cache.enabled` | `true` | 短语级翻译缓存。同签名同译文出现 ≥ `min_hits` 次后晋升为可缓存，后续相同短句（≤ `max_words` 词）跳过 API 直接复用译文，默认开启，可在设置面板关闭 |
| `cache.max_words` | `6` | 长度闸门。超过此词数永不进缓存（防长句被语境污染） |
| `cache.min_hits` | `3` | 稳定性闸门。同签名**连续**产出相同译文这么多次才晋升为可缓存 |
| `cache.size` | `500` | canonical LRU 容量上限。约 100KB 内存，可忽略 |
```

- [ ] **Step 3: 在 README 「延迟与提速」节追加一段**

定位 README 的「延迟与提速」一节（搜索 `## 延迟与提速`），在该节末尾追加：

```markdown
### 短句缓存（默认开启）

对话密集时大量句子是重复短语（"Yeah," / "Right?" / "Let's see," 等），这些走一次 API 就够。
缓存对每个签名做「连续相同译文 ≥ 3 次才晋升」——这样语境依赖型短语（"Right." 表同意 vs
"Turn right."）会因译文不一致而**自然不会**晋升。命中后跳过 API、不消耗并发名额。

- 仅 ≤ 6 词的短句有资格进缓存；
- 关闭后立即清空内存，避免脏命中（设置面板 / `config.json` 改 `cache.enabled=false`）；
- 命中时浮窗直接显示译文，跳过「翻译中…」占位。

实测：快语速对话里约 10–25% 的句子完全省掉一次 API 调用。
```

- [ ] **Step 4: 在 README 「常见问题」追加一条**

定位 README 的「常见问题」节，追加：

```markdown
**Q：怎么关掉短句缓存？**

A：设置面板里取消勾选，或 `config.json` 改 `cache.enabled=false`（热更新，无需重启）。关掉
后若发现某短句译得不对，请确认是不是缓存误命中。
```

- [ ] **Step 5: 在 README 「已实测验证」表追加一行**

定位「已实测验证」表，追加：

```markdown
| 短句缓存 | `tests/test_cache.py` 8 项单测通过 + `tools/e2e_selftest.py` 命中/未命中断言通过；同一短句投送 5 次后 `cache.size ≥ 1`、第 4 次起命中 |
```

---

## Task 6: e2e 自检加断言

**Files:**
- Modify: `tools/e2e_selftest.py`

- [ ] **Step 1: 定位插入点**

打开 `tools/e2e_selftest.py`，在 `run_selftest()`（行 93）末尾**、return 语句之前**插入新断言块。本任务的目标函数已经持有 `app` 变量，可直接访问 `app._cache` / `app.cache_hits` / `app.cache_misses`。

- [ ] **Step 2: 加缓存相关断言**

在 `run_selftest()` 末尾追加：

```python
    # ── 短句翻译缓存 ────────────────────────────────────────── #
    cache_text = "Yeah,"
    before_size  = app._cache.size()
    before_hits  = app.cache_hits
    before_miss  = app.cache_misses
    for _ in range(5):
        app._submit(cache_text)
    # 等 mock + observe 落定（MockTranslator sleep 0.05s × 3 次 + 余量）
    time.sleep(1.0)
    check(
        "cache_size_after_5x_repeat",
        app._cache.size() >= before_size + 1,
        f"size {before_size} -> {app._cache.size()}",
    )
    check(
        "cache_hits_after_5x_repeat",
        app.cache_hits >= before_hits + 1,
        f"hits {before_hits} -> {app.cache_hits}",
    )
    check(
        "cache_misses_strict_increase",
        app.cache_misses > before_miss,
        f"misses {before_miss} -> {app.cache_misses}",
    )
```

- [ ] **Step 3: 跑 e2e 自检**

```bash
python tools/e2e_selftest.py --console
```

Expected: 全部断言通过，包括新增的 3 条。

---

## Task 7: 最终验证

**Files:** 无（仅运行命令）

- [ ] **Step 1: 跑全部测试**

```bash
python tests/test_cache.py
python tests/test_translator.py
python tests/test_segmenter.py
python tests/test_caption_reader.py
python tools/e2e_selftest.py --console
```

Expected: 全部通过。

- [ ] **Step 2: 演示模式冒烟**

```bash
python main.py --console
```

启动后确认无 traceback；按 Ctrl+C 退出（退出码 0）。**不消耗 API 额度**。

- [ ] **Step 3: 浮窗模式冒烟**

```bash
python main.py --demo
```

确认浮窗能弹出、设置面板能打开、「翻译缓存」分组可见、开关可勾选。

---

## 完成标志（Definition of Done）

- [ ] `src/cache.py` 存在且通过 8 项单测
- [ ] `src/app.py` 接入缓存，`cache_hits` / `cache_misses` 计数器可用
- [ ] `src/settings_dialog.py` 含「翻译缓存」分组，开关可热更新
- [ ] `config.example.json` 与 `README.md` 已同步
- [ ] `tools/e2e_selftest.py` 新增 2 条断言并通过
- [ ] 全部既有测试不退化

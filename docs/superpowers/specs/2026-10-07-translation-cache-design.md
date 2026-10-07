# 短句翻译缓存 — 设计文档

**日期**：2026-10-07
**目标**：在快速说话时减少 API 请求数、稳定字幕滞后，让有限的并发名额留给真正的新内容。
**范围**：仅翻译缓存，不改动断句、推测翻译、并发主路径。

---

## 背景与目标

### 当前延迟构成（实测，README 已记录）

| 环节 | 实测 |
|---|---|
| 断句等待 | 平均 1.60s |
| 网络首字 | 0.96s |
| 网络波动 | 0.9s – 4.6s（同样的请求、同样的上下文） |

项目已有的提速措施：4 路并发、流式输出、`max_tokens` 封顶、推测翻译（86% 命中）、逗号从句、限流退避、backlog 丢弃、thinking 关闭。

### 还没解决的剩余瓶颈

快语速对话场景里大量句子是「重复短句」（"Yeah,", "Right?", "Let's see,", "I mean,", "Of course,", …）。这些走一次 API 就够，但目前每出现一次都吃一次并发名额，与真正需要翻译的新内容竞争。

### 目标量化

- **主要**：在快语速对话里，**至少 10% 的句子跳过 API 调用**（典型课堂 / 演讲对话里重复短语占比远高于 10%）。
- **次要**：浮窗里看到「直接出现译文」而非「翻译中…」占位，体验更流畅。
- **硬约束**：译文质量不退化。已验证稳定的翻译才进缓存；未达晋升门槛前不享受缓存。

---

## 设计

### 1. 架构与插入点

**新增模块**：`src/cache.py`，导出 `TranslationCache` 类（线程安全）。

**插入位置**：`src/app.py` 的 `TranslatorApp._submit(sentence)`。在把任务投进 `_jobs` 队列**之前**先问缓存：

```
                ┌──────────────┐
   segmenter ──▶│ _submit()    │
                │              │
                │  cache.lookup├─▶ 命中 → display.set_translation(row_id, ...)
                │              │           不投队列、不消耗并发
                │              │
                │  cache miss  │           未命中 → 走原路径
                └──────┬───────┘
                       ▼
                  _jobs.put(...) ─▶ 翻译线程 ─▶ DeepSeek ─▶ display
                                                  │
                                                  ▼
                                          cache.observe(sig, result)
```

观察发生在翻译线程拿到 API 响应**之后**，不进主路径延迟预算。

### 2. 资格判定（两道闸门）

1. **长度闸门**：归一化后词数 ≤ `cache.max_words`（默认 6）。长句永不进缓存。
2. **稳定性闸门**：同签名**连续**产出相同译文 ≥ `cache.min_hits` 次（默认 3）才晋升为可缓存。

> 为什么是「连续相同」而不是「累计次数」：语境依赖会让同签名产出不同译文（"Right." 表同意 vs "Turn right."）。让稳定性本身去过滤，不靠白名单。

### 3. 数据结构

```python
class TranslationCache:
    max_words: int
    min_hits:  int
    size:      int
    enabled:   bool

    _candidates: dict[str, Candidate]      # 还在观察中的签名
    _canonical:  OrderedDict[str, str]     # 已晋升的签名（LRU）
    _lock:       threading.Lock            # 线程安全

@dataclass
class Candidate:
    count: int            # 连续相同译文的次数
    last:  str            # 最近一次观察到的译文
```

### 4. 单次提交流程

```python
def _submit(self, sentence: str) -> None:
    # 已有：backlog 丢弃保护
    ...

    row_id = self._display.add(sentence)

    sig = _spec_key(sentence)                # 与 _consider_speculation 共用
    cached = self._cache.lookup(sig)
    if cached is not None:
        self._display.set_translation(row_id, cached, done=True)
        self.cache_hits += 1
        log.debug("译文（缓存命中）: %s", cached)
        return

    self.cache_misses += 1
    self._jobs.put((row_id, sentence))
    log.info("原文: %s", sentence)
```

### 5. 观察与晋升（在翻译线程收尾时）

```python
# _translator_loop 拿到 self._translator.translate(...) 之后
def _after_translate(self, source: str, translation: str) -> None:
    self._cache.observe(_spec_key(source), translation)
```

`observe()` 实现：

```python
def observe(self, sig: str, translation: str) -> None:
    if not self.enabled:
        return
    if not sig or not translation:
        return
    if len(sig.split()) > self.max_words:
        return                                # 长句直接不观察
    with self._lock:
        cand = self._candidates.get(sig)
        if cand is None:
            cand = Candidate(count=1, last=translation)
            self._candidates[sig] = cand
        elif cand.last == translation:
            cand.count += 1
        else:
            cand.count = 0                    # 同签名不同译文：重置，本次不计数
            cand.last = translation

        # 晋升判定放在所有分支之外，保证 min_hits=1 时首次观察也能晋升
        if cand.count >= self.min_hits and sig not in self._canonical:
            self._canonical[sig] = translation
            self._canonical.move_to_end(sig)
            # LRU 容量控制（字段名 _size_cap：不能叫 self.size，会遮蔽 size() 方法）
            while len(self._canonical) > self._size_cap:
                self._canonical.popitem(last=False)

        # 候选池也加个上限，避免长尾签名膨胀
        if len(self._candidates) > self._size_cap * 4:
            for k in list(self._candidates.keys()):
                if k not in self._canonical:
                    del self._candidates[k]
                    if len(self._candidates) <= self._size_cap * 2:
                        break
```

### 6. 行为细节

- **签名复用**：直接复用 `_spec_key()`（已在 `app.py` 中给推测翻译用），保证缓存键和推测键在同一归一化语义下。
- **错误不入缓存**：API 出错（`TranslationError`）时跳过 `observe()`，避免错误译文污染缓存。
- **禁用即清空**：设置面板关闭开关时调用 `cache.set_enabled(False)`，同步清空 `_canonical` 与 `_candidates`，防止脏命中。
- **命中 UX**：浮窗直接显示译文，跳过「翻译中…」占位。用户可感知到延迟下降。

### 7. 配置

**`config.json` 新增段**（默认开启）：

```json
"cache": {
    "enabled":   true,
    "max_words": 6,
    "min_hits":  3,
    "size":      500
}
```

- `enabled`：默认 `true`。
- `max_words` / `min_hits` / `size`：仅在 `config.json` 调整，重启生效。

**`src/config.py`**：在 `DEFAULTS["cache"]` 写默认值；`Config.get()` 自动覆盖。

**`config.example.json`**：补注释段。

### 8. 设置界面

**`src/settings_dialog.py`**：在 `open_settings_dialog` 的 Toplevel 里加一个分组「翻译缓存」（沿用已有的深色风格、字段控件、accent 配色）：

```
┌─ 翻译缓存 ────────────────────────────────────┐
│  [✓] 启用短语级翻译缓存                        │
│      重复出现 ≥ 3 次且 ≤ 6 词的句子直接复用    │
│      译文，省一次 API 调用                     │
│                                                │
│  命中缓存：[123]   未命中：[456]               │  ← 只读，运行时统计
└────────────────────────────────────────────────┘
```

- Tk `Checkbutton`（项目已经在用）。
- 运行时统计：每 500ms 通过回调拉一次 `TranslatorApp.cache_hits / cache_misses`，`Label` 刷新。
- 保存后立即生效：`_apply_cache_setting(enabled)` 调 `cache.set_enabled(enabled)`，关闭时清空内部状态。
- 「最大词数 / 最小命中数 / 容量」不进 UI，保持简单。

### 9. 诊断计数

**`src/app.py`**：在 `TranslatorApp.__init__` 里加：

```python
self.cache_hits   = 0
self.cache_misses = 0
```

每处命中/未命中同步增减。`self.cache_size()` 返回 `len(self._cache._canonical)`，给设置面板和 e2e 用。

### 10. 测试

**`tests/test_cache.py`**（新）—— 单元测试：

| 用例 | 验证点 |
|---|---|
| `test_lookup_empty` | 空 cache 永远 miss |
| `test_promote_after_min_hits` | 同 sig 同译文 3 次 → 第 4 次命中 |
| `test_promote_not_after_two` | 同 sig 同译文 2 次仍 miss |
| `test_promote_reset_on_diff` | 同 sig 不同译文 → 计数重置 |
| `test_long_text_never_cached` | > max_words 词的 sig 永不进 canonical |
| `test_lru_eviction` | 超 size 后最旧 canonical 被踢出 |
| `test_disable_clears_cache` | `set_enabled(False)` 后内存清空 |
| `test_thread_safety` | 多线程并发 observe/lookup 不崩、不脏读 |

**`tests/test_cache.py`** —— 加 1 个集成用例：构造 `TranslatorApp(mock=True)`，投 5 条同一短句，断言 `mock.translate` 只被调用 2 次（前两次走 API 触发晋升、第 3 次起走缓存）。

**`tools/e2e_selftest.py`** —— 加断言：
- 脚本化字幕里加 5 条同一短句，断言 `cache_hits >= 3`、`cache_misses` 仅为前两次。
- 加 1 条长句（>6 词），断言 `cache_hits` 没增加、`cache_misses += 1`。

**`tools/measure_latency.py`** —— 加 `--cache on/off` 选项，对比：
- 总请求数（API 调用次数）
- 翻译滞后平均值 / P95

### 11. 文档

**`README.md`** 三处更新：
1. 「延迟与提速」节加一段：短句缓存的说明。
2. 配置表加 4 行。
3. 「常见问题」加一条：怎么关闭短句缓存。

「已实测验证」表加一行。

---

## 不在范围内（明确不做）

- 跨进程 / 持久化缓存（重启清空，本次会话内有效）。
- 上文感知的缓存（同签名强制无上下文翻译；用户已接受轻微质量取舍，但**完全不感知上下文**会出问题，所以本设计通过「连续相同译文」闸门把上下文依赖自然过滤掉）。
- 短语白名单 / 黑名单（让稳定性本身去过滤，比白名单更可靠）。
- 句子合并请求 / 自适应 worker（属于方案 B，本次不做）。

---

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| 缓存误命中导致译文错误 | 「连续相同 3 次」+ 「≤6 词」两道闸门；可通过设置面板一键关闭 |
| 长尾签名撑爆内存 | `candidates` 上限 = `size * 4`；`canonical` 上限 = `size`（默认 500，总内存 ≈ 100KB） |
| 并发读写脏数据 | `threading.Lock` 包裹所有读写 |
| 设置面板性能 | 统计刷新间隔 500ms，不影响主路径 |

回滚方式：设置面板取消勾选，或 `config.json` 改 `cache.enabled=false`（无需重启程序，已热更新）。

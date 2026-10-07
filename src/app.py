#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""主流程编排：采集 → 断句 → 翻译 → 展示。

线程模型
--------
* **采集线程**：轮询 UI Automation，读原始字幕 → 断句 → 投翻译任务
* **翻译线程**：串行消费任务（保证译文顺序），携带上文调用 DeepSeek
* **展示线程**：把最新内容推给浮窗队列（仅在浮窗模式下启用）
* **主线程**：跑 Tk 事件循环，或跑控制台输出
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from .caption_reader import (
    STATUS_CAPTIONING,
    STATUS_NOT_RUNNING,
    STATUS_TEXT,
    CaptionReader,
    ScriptedCaptionReader,
    kill_live_captions,
)
from .config import Config, save_overlay_position
from .hotkeys import MOD_ALT, MOD_CONTROL, VK, GlobalHotkeys
from .overlay import Overlay
from .segmenter import CaptionSegmenter
from .translator import MockTranslator, TranslationError, create_translator
from .cache import TranslationCache
from .phrasebook import Phrasebook

log = logging.getLogger("app")

_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def _spec_key(text: str) -> str:
    """把文本归一化成「词序列」，用来判断两次文本是否只是标点差异。

    推测翻译与短语缓存都靠它匹配：定稿时的句子往往只是推测时那句补了个句号。
    **空白也要折叠**——实时字幕偶尔会给出连续空格，不折叠就会漏命中。
    """
    stripped = _PUNCTUATION.sub("", text or "")
    return _WHITESPACE.sub(" ", stripped).casefold().strip()


@dataclass
class Speculation:
    """一次「提前翻译」。句子定稿时若命中它，就不用再等一次网络往返。"""

    words: str
    source: str
    done: threading.Event = field(default_factory=threading.Event)
    started: bool = False
    translation: str = ""
    failed: bool = False


# --------------------------------------------------------------------------- #
class RawCaptionLogger:
    """把原始字幕落盘，便于事后调参（stable_ms / 断句规则）。"""

    def __init__(self, directory: Path) -> None:
        self.enabled = True
        try:
            directory.mkdir(parents=True, exist_ok=True)
            name = time.strftime("captions-%Y%m%d-%H%M%S.log")
            self.path = directory / name
            self._handle = self.path.open("w", encoding="utf-8")
        except OSError as exc:
            log.warning("无法写入字幕日志：%s", exc)
            self.enabled = False
            self._handle = None

    def write(self, raw: str) -> None:
        if not self.enabled or self._handle is None:
            return
        stamp = time.strftime("%H:%M:%S")
        self._handle.write(f"[{stamp}] {raw!r}\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle:
            try:
                self._handle.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
@dataclass
class Row:
    row_id: int
    source: str
    translation: str = ""
    done: bool = False


class DisplayModel:
    """浮窗内容的共享状态，线程安全。"""

    def __init__(self, max_rows: int = 8) -> None:
        self._rows: deque[Row] = deque(maxlen=max_rows)
        self._lock = threading.Lock()
        self._next_id = 1

    def add(self, source: str) -> int:
        with self._lock:
            row_id = self._next_id
            self._next_id += 1
            self._rows.append(Row(row_id=row_id, source=source))
            return row_id

    def set_translation(self, row_id: int, text: str, done: bool) -> None:
        with self._lock:
            for row in self._rows:
                if row.row_id == row_id:
                    row.translation = text
                    row.done = done
                    return

    def snapshot(self, count: int) -> list[tuple[str, str]]:
        """取最后若干条用于显示。译文还没到的行返回空串，由浮窗决定怎么占位。"""
        with self._lock:
            rows = list(self._rows)[-count:]
            return [(row.source, row.translation) for row in rows]

    def recent(self, count: int) -> list[Row]:
        with self._lock:
            return list(self._rows)[-count:]

    @property
    def total_added(self) -> int:
        """累计提交翻译的句数（不受保留条数上限影响）。

        注意 :meth:`recent` 只会返回最后 ``maxlen`` 条，所以「提交了多少句」
        必须看这个计数，不能拿 ``recent`` 的长度去比。
        """
        with self._lock:
            return self._next_id - 1


# --------------------------------------------------------------------------- #
class TranslatorApp:
    def __init__(self, cfg: Config, mock: bool = False, demo: bool = False) -> None:
        self.cfg = cfg
        self.mock = mock
        self.demo = demo
        self._stop = threading.Event()

        max_sentences = max(1, int(cfg.get("overlay.max_sentences", 2)))
        self._display = DisplayModel(max_rows=max(4, max_sentences * 4))
        self._jobs: queue.Queue = queue.Queue()
        # 翻译上文。**按 row_id 存**而不是按完成顺序追加：
        # 并发翻译时后一句可能先翻完，按完成顺序取上文会让模型以为上一句是
        # 别的内容，译文读起来就会「错位」。
        self._context_size = max(1, int(cfg.get("translate.context_sentences", 3)))
        self._context: dict[int, tuple[str, str]] = {}
        self._context_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._status_text = "正在启动…"

        self._segmenter = CaptionSegmenter(
            stable_ms=int(cfg.get("captions.stable_ms", 1400)),
            clause_min_words=int(cfg.get("captions.clause_min_words", 0)),
        )
        if demo:
            self._reader = ScriptedCaptionReader(speed=float(cfg.get("demo.speed", 1.0)))
        else:
            self._reader = CaptionReader(
                poll_interval_ms=int(cfg.get("captions.poll_interval_ms", 120)),
                auto_launch=bool(cfg.get("captions.auto_launch", True)),
                reconnect_interval_seconds=float(
                    cfg.get("captions.reconnect_interval_seconds", 2.0)
                ),
                log=log.info,
            )
        self._translator = create_translator(cfg, mock=mock)
        self._overlay: Overlay | None = None
        self._hotkeys: GlobalHotkeys | None = None
        self._raw_logger: RawCaptionLogger | None = None
        if cfg.get("captions.log_raw_text", True):
            self._raw_logger = RawCaptionLogger(cfg.resolve_path(cfg.get("captions.log_dir", "logs")))
        self._threads: list[threading.Thread] = []
        self._worker_count = 1  # 由 _start_threads 按配置覆盖

        # 推测翻译：在句子定稿前先把「词已稳定」的半句送去翻译。
        # 实测命中率 86%，等于把 1.6s 的断句等待藏起来。代价是请求数约 2 倍。
        self._speculate_enabled = bool(cfg.get("translate.speculate", True))
        self._speculate_ms = max(100, int(cfg.get("translate.speculate_ms", 500)))
        self._speculations: dict[str, Speculation] = {}
        self._spec_lock = threading.Lock()
        self._spec_queue: queue.Queue = queue.Queue(maxsize=1)
        self._spec_words = ""
        self._spec_since = 0.0
        # 诊断计数：触发 / 命中。命中意味着那句译文在定稿时就已就绪，省下一次往返。
        self.speculation_fired = 0
        self.speculation_hits = 0

        # 短句翻译缓存：AI 学来的。多数票达到 min_hits 且占比 ≥ majority 才晋升，
        # 后续相同短句（≤ max_words 词）跳过 API 直接复用译文。
        self._cache = TranslationCache(
            max_words = int(cfg.get("cache.max_words", 6)),
            min_hits  = int(cfg.get("cache.min_hits", 3)),
            size      = int(cfg.get("cache.size", 500)),
            majority  = float(cfg.get("cache.majority", 0.75)),
            min_samples = int(cfg.get("cache.min_samples", 5)),
        )
        # enabled 是属性不是构造参数（hot-update 用 set_enabled）
        self._cache.set_enabled(bool(cfg.get("cache.enabled", True)))
        self.cache_hits   = 0
        self.cache_misses = 0

        # 内置短语包：翻译链路的第一层，命中即用、零延迟零成本。
        # 只收录脱离上下文也唯一正确的短语，所以不需要运行时再用 AI 复核。
        self._phrasebook = Phrasebook.load(cfg.get("phrasebook.path") or None)
        self._phrasebook_enabled = bool(cfg.get("phrasebook.enabled", True))
        self.phrasebook_hits = 0
        log.info(
            "短语包已加载：%d 条（%s）",
            self._phrasebook.size(),
            self._phrasebook.source,
        )

    # ------------------------------------------------------------------ #
    def _status(self) -> str:
        with self._status_lock:
            return self._status_text

    def _set_status(self, text: str) -> None:
        with self._status_lock:
            self._status_text = text

    # ------------------------------------------------------------------ #
    # 采集
    # ------------------------------------------------------------------ #
    def _reader_loop(self) -> None:
        empty_reads = 0
        last_raw: str | None = None
        needs_prime = True

        while not self._stop.is_set():
            try:
                state = self._reader.read()
            except Exception as exc:  # 采集层不应让线程崩掉
                log.debug("采集异常：%s", exc)
                self._set_status(f"采集异常：{exc}")
                self._stop.wait(0.5)
                continue

            status_text = STATUS_TEXT.get(state.status, "")
            if state.status == STATUS_NOT_RUNNING and not getattr(self._reader, "auto_launch", True):
                # 关掉「自动启动实时字幕」后如果字幕没开，界面上只有一句
                # 「实时字幕未运行」——用户不知道是自己关的，也不知道怎么办。
                status_text = "实时字幕未运行：请手动打开它，或在设置里开启「自动启动实时字幕」"
            self._set_status(status_text)

            if state.has_text:
                empty_reads = 0
                if state.text != last_raw:
                    last_raw = state.text
                    if self._raw_logger:
                        self._raw_logger.write(state.text)
                if needs_prime:
                    # 实时字幕的转录是整场会话累积的。程序若在会话中途启动，
                    # 直接喂给断句器会把历史内容全部送去翻译，这里先跳过。
                    self._segmenter.prime(state.text)
                    needs_prime = False
                    log.info("已跳过实时字幕中已有的历史转录")
                else:
                    for sentence in self._segmenter.feed(state.text):
                        self._submit(sentence)
                    self._consider_speculation()
            else:
                empty_reads += 1
                # 字幕确实停了才收尾，避免把句中的短暂空档误当结束
                if state.status != STATUS_CAPTIONING or empty_reads >= 5:
                    for sentence in self._segmenter.flush():
                        self._submit(sentence)
                    last_raw = None

            self._stop.wait(self._reader.poll_interval)

    def _submit(self, sentence: str) -> None:
        # 保护：翻译跟不上说话速度时（网络慢、对话密集），待翻译队列会越积越长，
        # 字幕就会越来越滞后。这里丢掉最旧的待翻译句，保证显示的始终贴近当前时刻。
        limit = max(1, int(self.cfg.get("translate.max_backlog", 3)))
        while self._jobs.qsize() >= limit:
            try:
                dropped_id, dropped_text = self._jobs.get_nowait()
            except queue.Empty:
                break
            self._display.set_translation(dropped_id, "（积压跳过）", done=True)
            log.warning("翻译积压，跳过一句：%s", dropped_text)

        row_id = self._display.add(sentence)
        signature = _spec_key(sentence)

        # 第一层：内置短语包 —— 零延迟、零成本、不占并发名额
        if self._phrasebook_enabled:
            preset = self._phrasebook.lookup(signature)
            if preset is not None:
                self._display.set_translation(row_id, preset, done=True)
                self._remember(row_id, sentence, preset)
                self.phrasebook_hits += 1
                log.debug("译文（短语包命中）: %s", preset)
                return

        # 第二层：AI 学到的短句缓存 —— 跳过 API，不消耗并发名额
        cached = self._cache.lookup(signature)
        if cached is not None:
            self._display.set_translation(row_id, cached, done=True)
            # 与正常路径保持一致：这句也要进入「上文」，否则后续翻译看不到它，
            # 缓存命中越多、上下文越残缺
            self._remember(row_id, sentence, cached)
            self.cache_hits += 1
            log.debug("译文（缓存命中）: %s", cached)
            return

        self.cache_misses += 1
        self._jobs.put((row_id, sentence))
        log.info("原文: %s", sentence)

    # ------------------------------------------------------------------ #
    # 翻译
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # 推测翻译
    # ------------------------------------------------------------------ #
    def _context_snapshot(self) -> list[tuple[str, str]]:
        """取最近若干句作为翻译上文，**按提交顺序**排列。

        并发翻译时完成顺序是乱的，必须按 row_id 排序后再取末尾若干句，
        否则模型看到的「上一句」可能是未来的某一句。
        """
        with self._context_lock:
            items = sorted(self._context.items())[-self._context_size :]
        return [pair for _, pair in items]

    def _remember(self, row_id: int, source: str, translation: str) -> None:
        """把一句已经确定的译文记入上文（供后续句子参考）。"""
        with self._context_lock:
            self._context[row_id] = (source, translation)
            # 只留最近的一小段，避免长视频里无限增长
            if len(self._context) > self._context_size * 4:
                for key in sorted(self._context)[: -self._context_size]:
                    del self._context[key]

    def _consider_speculation(self) -> None:
        """待定内容的「词」稳定足够久，就先把它送去翻译。

        实测命中率 86%：定稿时那句往往只是这里补了个句号，于是译文已经就绪，
        等于把断句等待（约 1.6s）整个藏起来了。
        """
        if not self._speculate_enabled:
            return
        pending = self._segmenter.pending
        key = _spec_key(pending)
        now = time.monotonic()
        if not key:
            self._spec_words = ""
            return
        if key != self._spec_words:
            self._spec_words = key
            self._spec_since = now
            return
        if now - self._spec_since < self._speculate_ms / 1000.0:
            return

        with self._spec_lock:
            if key in self._speculations:
                return
            speculation = Speculation(words=key, source=pending.strip())
            # 只保留最近几条，避免长视频里无限增长
            while len(self._speculations) >= 6:
                self._speculations.pop(next(iter(self._speculations)), None)
            self._speculations[key] = speculation
            self.speculation_fired += 1

        # 队列里只保留最新一个：更新的推测更有价值
        try:
            self._spec_queue.put_nowait(speculation)
        except queue.Full:
            try:
                self._spec_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._spec_queue.put_nowait(speculation)
            except queue.Full:
                pass

    def _take_speculation(self, source: str) -> Speculation | None:
        """取出与这句匹配的推测（取走即视为消费）。"""
        if not self._speculate_enabled:
            return None
        with self._spec_lock:
            return self._speculations.pop(_spec_key(source), None)

    def _speculation_loop(self) -> None:
        while True:
            item = self._spec_queue.get()
            if item is None:
                return
            with self._spec_lock:
                # 已被取用、或已被更新的推测顶掉，就别白花额度了
                if self._speculations.get(item.words) is not item:
                    item.done.set()
                    continue
            item.started = True
            try:
                item.translation = self._translator.translate(item.source, self._context_snapshot())
            except Exception as exc:
                item.failed = True
                log.debug("推测翻译失败（忽略）：%s", exc)
            finally:
                item.done.set()

    # ------------------------------------------------------------------ #
    # 翻译
    # ------------------------------------------------------------------ #
    def _translator_loop(self, worker_index: int) -> None:
        """翻译工作线程。可以有多个并发跑，用来抵消 API 响应时间的波动。

        实测 DeepSeek 单句响应在 0.9s–4.6s 之间波动（同样的请求、同样的上下文）。
        单个线程串行翻译时，只要 API 慢到 3s/句、视频又每 2s 出一句，队列就会
        越积越长，字幕越来越滞后。多开几个线程是唯一能真正提高吞吐的办法。

        并发带来的取舍：每个线程拿到的「上文」是各自取快照时最近若干句的译文，
        顺序可能略有出入。上文只是提升质量用的，不影响正确性。
        """
        consecutive_errors = 0
        while True:
            job = self._jobs.get()
            if job is None:
                return
            row_id, source = job

            # 先看有没有可用的推测结果：它比现在才发请求更早完成。
            # 只等「已经在跑」的那一个；还排着队没开始的就丢掉走正常翻译，
            # 否则等待可能比重新翻一次还久。
            speculation = self._take_speculation(source)
            if speculation is not None:
                if not speculation.started:
                    speculation.done.set()
                elif speculation.done.wait(timeout=8.0) and not speculation.failed:
                    if speculation.translation:
                        self._display.set_translation(row_id, speculation.translation, done=True)
                        self._remember(row_id, source, speculation.translation)
                        self.speculation_hits += 1
                        # 推测译文也参与缓存晋升
                        self._cache.observe(_spec_key(source), speculation.translation)
                        log.info("译文（推测命中）: %s", speculation.translation)
                        consecutive_errors = 0
                        continue

            context = self._context_snapshot()
            buffer: list[str] = []

            def on_delta(piece: str) -> None:
                buffer.append(piece)
                self._display.set_translation(row_id, "".join(buffer).strip(), done=False)

            try:
                translation = self._translator.translate(source, context, on_delta=on_delta)
                self._display.set_translation(row_id, translation, done=True)
                self._remember(row_id, source, translation)
                # 拿到 API 响应后，让缓存观察稳定性
                self._cache.observe(_spec_key(source), translation)
                consecutive_errors = 0
                log.info("译文: %s", translation)
            except TranslationError as exc:
                consecutive_errors += 1
                self._display.set_translation(row_id, f"⚠ {exc}", done=True)
                log.error("%s", exc)
                if consecutive_errors >= 3:
                    log.error("连续翻译失败，暂停 5 秒后继续…")
                    self._stop.wait(5.0)
                    consecutive_errors = 0

    # ------------------------------------------------------------------ #
    # 展示
    # ------------------------------------------------------------------ #
    def _display_loop(self) -> None:
        """把最新内容推给浮窗（仅浮窗模式运行）。"""
        last_signature = None
        count = max(1, int(self.cfg.get("overlay.max_sentences", 2)))
        while not self._stop.is_set():
            rows = self._display.snapshot(count)
            status = self._status()
            signature = (tuple(rows), status)
            if signature != last_signature:
                last_signature = signature
                if self._overlay is not None:
                    self._overlay.post(rows, status)
            self._stop.wait(0.08)

    def _console_loop(self) -> None:
        """控制台模式：每句定稿后打印一次，便于排查问题。"""
        print("AI 视频翻译 · 控制台模式（Ctrl+C 退出）")
        print("-" * 78)
        printed: set[int] = set()
        last_status = None
        while not self._stop.is_set():
            for row in self._display.recent(30):
                if row.done and row.row_id not in printed:
                    printed.add(row.row_id)
                    print(f"\n原文  {row.source}\n译文  {row.translation}")
            status = self._status()
            if status != last_status:
                last_status = status
                print(f"[状态] {status}")
            self._stop.wait(0.1)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def _start_threads(self) -> None:
        workers = max(1, int(self.cfg.get("translate.workers", 3)))
        self._worker_count = workers
        targets: list[tuple] = [(self._reader_loop, "reader")]
        for index in range(workers):
            targets.append((partial(self._translator_loop, index), f"translator-{index}"))
        if self._speculate_enabled:
            targets.append((self._speculation_loop, "speculation"))
        if self.cfg.get("overlay.enabled", True):
            targets.append((self._display_loop, "display"))
        for target, name in targets:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("已启动 %d 个翻译线程", workers)

    def _start_hotkeys(self) -> None:
        overlay = self._overlay

        def ui(name: str, *args):
            """热键回调跑在热键线程里，界面操作必须转交给主线程。"""
            return lambda: overlay.post_command(name, *args) if overlay else None

        bindings = [
            (MOD_CONTROL | MOD_ALT, VK["Q"], self.quit),
            (MOD_CONTROL | MOD_ALT, VK["T"], ui("click_through")),
            (MOD_CONTROL | MOD_ALT, VK["H"], ui("toggle_visible")),
            (MOD_CONTROL | MOD_ALT, VK["UP"], ui("nudge", -30)),
            (MOD_CONTROL | MOD_ALT, VK["DOWN"], ui("nudge", 30)),
        ]
        self._hotkeys = GlobalHotkeys(bindings)
        self._hotkeys.start()
        self._hotkeys.wait_ready(2.0)
        log.info("已注册 %d 个全局热键", self._hotkeys.registered_count)

    # ------------------------------------------------------------------ #
    def request_quit(self) -> None:
        """线程安全地请求退出。

        这里只置停止标志并关浮窗；收尾（把最后一句话翻完、给工作线程发结束信号）
        统一由 :meth:`_shutdown` 负责，避免过早发出结束信号而丢掉最后一句。
        """
        self._stop.set()
        if self._overlay is not None:
            self._overlay.request_close()

    # 热键回调用的别名
    quit = request_quit

    # ------------------------------------------------------------------ #
    # 浮窗上的按钮
    # ------------------------------------------------------------------ #
    def _open_settings(self) -> None:
        """⚙ 按钮：打开设置面板填写 / 更换 API Key。"""
        if self._overlay is None:
            return
        try:
            from .settings_dialog import open_settings_dialog

            def _on_cache_toggle(enabled: bool) -> None:
                self._cache.set_enabled(enabled)
                if not enabled:
                    self.cache_hits = 0
                    self.cache_misses = 0

            def _cache_stats() -> tuple[int, int, int]:
                return (self.cache_hits, self.cache_misses, self.phrasebook_hits)

            def _apply() -> None:
                """保存后把「外观」类改动热应用到浮窗。

                界面操作必须在主线程做：设置面板本身就跑在主线程，
                所以这里直接重建面板是安全的（不是从工作线程调过来的）。
                """
                if self._overlay is not None:
                    self._overlay.apply_settings()

            open_settings_dialog(
                self._overlay._root,
                self.cfg,
                on_saved=self._apply_api_key,
                on_cache_toggle=_on_cache_toggle,
                cache_stats_provider=_cache_stats,
                on_apply=_apply,
            )
        except Exception as exc:
            log.error("打开设置面板失败：%s", exc)

    def _apply_api_key(self, api_key: str, model: str) -> None:
        """设置面板保存成功后，让正在跑的翻译立刻用上新值（不必重启）。"""
        self._translator.api_key = api_key
        if model:
            self._translator.model = model
        log.info("API Key 已更新（模型 %s）", model or self._translator.model)

    def _close_from_overlay(self) -> None:
        """✕ 按钮：关闭程序，并按配置一并关掉 Windows 实时字幕。"""
        self._stop.set()
        if bool(self.cfg.get("overlay.close_livecaptions", True)):
            if kill_live_captions():
                log.info("已同时关闭 Windows 实时字幕")
        if self._overlay is not None:
            self._overlay.request_close()

    def run(self) -> int:
        self._start_threads()
        exit_code = 0
        try:
            if self.cfg.get("overlay.enabled", True):
                self._overlay = Overlay(
                    self.cfg,
                    on_quit=self._stop.set,
                    on_settings=self._open_settings,
                    on_close=self._close_from_overlay,
                )
                self._start_hotkeys()
                print("浮窗已启动。")
                # 控制台输出只用 GBK 能编码的字符：中文 Windows 控制台默认 cp936，
                # 一旦输出被重定向（管道/文件），⚙ ✕ 这类符号会直接抛 UnicodeEncodeError，
                # 而这几行在浮窗启动之前，会导致浮窗根本出不来。
                print("  右上角按钮：[设置] 填 API Key   [关闭] 退出（并关掉系统实时字幕）")
                print("  面板可以直接用鼠标拖动，位置会记住")
                print("  热键：Ctrl+Alt+Q 退出 / T 切换鼠标穿透 / H 显示隐藏 / 上↓ 移动")
                self._overlay.run()
            else:
                self._console_loop()
        except KeyboardInterrupt:
            print("\n收到中断信号，正在退出…")
        finally:
            exit_code = self._shutdown()
        return exit_code

    def _shutdown(self) -> int:
        self._stop.set()
        if self._hotkeys:
            self._hotkeys.stop()
        # 记住用户拖动后的位置，下次启动还在原地
        if self._overlay is not None and self.cfg.path is not None:
            position = self._overlay.manual_position
            if position:
                try:
                    save_overlay_position(self.cfg.path, position[0], position[1])
                except OSError as exc:
                    log.debug("保存浮窗位置失败：%s", exc)
        # 先把最后一句话交出去，再给工作线程发结束信号——顺序反了会丢掉最后一句
        for sentence in self._segmenter.flush():
            self._submit(sentence)
        for _ in range(max(1, self._worker_count)):
            self._jobs.put(None)
        if self._speculate_enabled:
            try:
                self._spec_queue.put_nowait(None)
            except queue.Full:
                try:
                    self._spec_queue.get_nowait()
                    self._spec_queue.put_nowait(None)
                except queue.Empty:
                    pass

        deadline = time.monotonic() + 8.0
        for thread in self._threads:
            remaining = max(0.1, deadline - time.monotonic())
            thread.join(timeout=remaining)
        if self._raw_logger:
            self._raw_logger.close()
        try:
            self._translator.close()
        except Exception:
            pass
        if self.mock:
            log.info("（使用了模拟翻译器）")
        return 0

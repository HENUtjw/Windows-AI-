#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全局热键（不依赖任何第三方库）。

为什么需要
----------
浮窗默认开启「鼠标穿透」——点击直接落到下面的视频上，这样看视频时不会误触。
代价是鼠标点不到浮窗本身，所以退出、移动、切换穿透都必须靠全局热键。

实现要点：``RegisterHotKey`` 绑定的热键消息只会投递到**注册它的那个线程**的
消息队列，因此这里单开一个线程并跑自己的 ``GetMessage`` 循环。
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable

user32 = ctypes.windll.user32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# 虚拟键码
VK = {
    "Q": 0x51, "T": 0x54, "H": 0x48, "C": 0x43, "SPACE": 0x20,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "0": 0x30, "1": 0x31, "2": 0x32,
}


class GlobalHotkeys(threading.Thread):
    """在后台线程注册全局热键。"""

    def __init__(self, bindings: list[tuple[int, int, Callable[[], None]]]) -> None:
        """bindings: [(modifiers, virtual_key, callback), ...]"""
        super().__init__(daemon=True, name="hotkeys")
        self._bindings = bindings
        self._thread_id: int | None = None
        self._registered: list[int] = []
        self._ready = threading.Event()

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
        for index, (modifiers, key, _) in enumerate(self._bindings, start=1):
            if user32.RegisterHotKey(None, index, modifiers | MOD_NOREPEAT, key):
                self._registered.append(index)
        self._ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message != WM_HOTKEY:
                continue
            index = int(message.wParam) - 1
            if 0 <= index < len(self._bindings):
                try:
                    self._bindings[index][2]()
                except Exception:
                    pass

        for index in self._registered:
            user32.UnregisterHotKey(None, index)

    # ------------------------------------------------------------------ #
    def wait_ready(self, timeout: float = 2.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    @property
    def registered_count(self) -> int:
        return len(self._registered)

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Windows 实时字幕采集器（UI Automation）。

实测结论（Windows 11 build 26200）
--------------------------------
窗口类名固定为 ``LiveCaptionsDesktopWindow``，标题为「实时辅助字幕」。

UI 树是**动态**的::

    待机时：
      [TextControl] aid='ReadyToCaptionTextBlock'  name='已准备好在 简体中文(中国大陆) 中显示实时字幕'
      [ButtonControl] aid='SettingsButton'
      [ButtonControl] aid='CloseButton'

    字幕滚动时：
      [TextControl] aid='CaptionsTextBlock'        name='<字幕文本>'

也就是说 ``CaptionsTextBlock`` **只在字幕真正产生时才存在**。因此不能硬编码
单一 ID，必须实现回退链，否则「没字幕」和「字幕还没来」会被混为一谈。

另一个实测要点：从桌面根节点枚举窗口在这个环境下不一定可靠，
而 ``FindWindowW`` 按类名定位窗口非常稳定，所以采用「先拿 HWND，再由句柄取元素」。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import uia_bootstrap  # noqa: E402,F401  (导入即生效，必须早于 uiautomation)

import uiautomation as auto  # noqa: E402

WINDOW_CLASS = "LiveCaptionsDesktopWindow"
CAPTION_AUTOMATION_IDS = ("CaptionsTextBlock",)
STATUS_AUTOMATION_IDS = ("ReadyToCaptionTextBlock", "ErrorTextBlock")

# 已知的界面元素：这些绝不是字幕，启发式回退必须排除它们，
# 否则「已准备好在…中显示实时字幕」这类提示会被当成字幕送去翻译。
NON_CAPTION_AUTOMATION_IDS = frozenset(
    set(STATUS_AUTOMATION_IDS) | {"PrivacyTeachingTip", "SettingsButton", "CloseButton"}
)

# 状态机取值
STATUS_NOT_RUNNING = "not-running"
STATUS_LAUNCHING = "launching"
STATUS_IDLE = "idle"
STATUS_CAPTIONING = "captioning"
STATUS_NO_ELEMENT = "no-element"

STATUS_TEXT = {
    STATUS_NOT_RUNNING: "实时字幕未运行",
    STATUS_LAUNCHING: "正在启动实时字幕…",
    STATUS_IDLE: "实时字幕已就绪，但还没有识别到语音",
    STATUS_CAPTIONING: "正在识别",
    STATUS_NO_ELEMENT: "已连上实时字幕窗口，但找不到文本元素",
}


@dataclass
class CaptionState:
    text: str
    status: str

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


def _windows_dir() -> str:
    return os.environ.get("WINDIR") or r"C:\Windows"


def live_captions_exe() -> str:
    return os.path.join(_windows_dir(), "System32", "LiveCaptions.exe")


def kill_live_captions() -> bool:
    """关闭 Windows 实时字幕。

    用户希望「关掉翻译浮窗时，系统实时字幕也一起关掉」——否则屏幕上会剩一个
    只显示英文、没人管的字幕窗。
    """
    try:
        result = subprocess.run(
            ["taskkill", "/IM", "LiveCaptions.exe", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _find_window_handle() -> int:
    return int(ctypes.windll.user32.FindWindowW(WINDOW_CLASS, None) or 0)


# uiautomation 是线程亲和（thread-affine）的：COM 要按线程初始化，
# 且 Control 对象不能跨线程使用。这里按线程记录初始化状态。
_thread_state = threading.local()


def _looks_like_icon(name: str) -> bool:
    """Segoe 图标字体的私有区字符（设置/关闭按钮上的图标）。"""
    return bool(name) and all("\ue000" <= ch <= "\uf8ff" for ch in name)


class CaptionReader:
    """读取 Windows 实时字幕当前显示的文本。"""

    def __init__(
        self,
        poll_interval_ms: int = 120,
        auto_launch: bool = True,
        reconnect_interval_seconds: float = 2.0,
        log=None,
    ) -> None:
        self.poll_interval = max(30, int(poll_interval_ms)) / 1000.0
        self.auto_launch = auto_launch
        self.reconnect_interval = max(0.5, float(reconnect_interval_seconds))
        self._log = log or (lambda message: None)

        self._hwnd: int = 0
        self._window = None
        self._text_element = None
        self._status_element = None
        self._last_connect_attempt: float = 0.0
        self._last_launch_attempt: float = 0.0
        self.launch_cooldown = 15.0
        auto.SetGlobalSearchTimeout(1.0)

    # ------------------------------------------------------------------ #
    # 线程与 COM
    # ------------------------------------------------------------------ #
    def ensure_thread_ready(self) -> bool:
        """确保**当前线程**已初始化 COM。所有 UIA 访问前都必须先过这里。

        为什么必须有这一步
        ------------------
        ``uiautomation`` 基于 comtypes，要求每个使用它的线程先调用
        ``CoInitializeEx``。采集循环跑在工作线程里，漏掉这一步的后果非常隐蔽：
        **不会抛异常**，只会不停打印「尚未调用 CoInitialize /
        Can not load UIAutomationCore.dll」，然后永远读不到任何字幕——
        表现为「程序在跑、浮窗在、但一条字幕都没有」。

        另外注意：``Control`` 对象不能跨线程使用，所以采集必须在同一个线程里完成。
        本类的元素缓存天然满足这一点（只在调用 ``read`` 的线程里创建与使用）。
        """
        if getattr(_thread_state, "com_ready", False):
            return True
        try:
            auto.InitializeUIAutomationInCurrentThread()
        except Exception as exc:
            self._log(f"当前线程初始化 COM 失败：{exc}")
            return False
        _thread_state.com_ready = True
        return True

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def is_running(self) -> bool:
        return _find_window_handle() != 0

    def launch(self) -> bool:
        """启动 LiveCaptions.exe。已运行则直接返回 True。"""
        if self.is_running():
            return True
        exe = live_captions_exe()
        if not os.path.exists(exe):
            self._log(f"找不到实时字幕程序：{exe}")
            return False
        try:
            creation = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([exe], creationflags=creation, close_fds=True)
            self._launch_requested = True
            self._log("已请求启动 Windows 实时字幕，等待窗口出现…")
            return True
        except OSError as exc:
            self._log(f"启动实时字幕失败：{exc}")
            return False

    def connect(self) -> bool:
        """尝试连接实时字幕窗口。返回是否连接成功。"""
        if not self.ensure_thread_ready():
            return False
        hwnd = _find_window_handle()
        if not hwnd:
            self._detach()
            return False
        if hwnd != self._hwnd or self._window is None:
            self._detach()
            try:
                window = auto.ControlFromHandle(hwnd)
            except Exception as exc:
                self._log(f"由句柄取窗口元素失败：{exc}")
                return False
            if window is None:
                return False
            self._hwnd = hwnd
            self._window = window
            self._log(f"已连接实时字幕窗口 (hwnd={hwnd})")
        self._locate_elements()
        return True

    def _detach(self) -> None:
        self._hwnd = 0
        self._window = None
        self._text_element = None
        self._status_element = None

    # ------------------------------------------------------------------ #
    # 元素定位
    # ------------------------------------------------------------------ #
    def _find_by_automation_id(self, automation_id: str):
        if self._window is None:
            return None
        try:
            control = auto.Control(
                searchFromControl=self._window, AutomationId=automation_id, searchDepth=16
            )
            if control.Exists(0.4, 0.1):
                return control
        except Exception:
            return None
        return None

    def _locate_elements(self) -> None:
        """查找字幕文本元素与状态提示元素。

        查找顺序是有意设计的，不能随意调换：

        1. 先认已知的字幕元素 ``CaptionsTextBlock``；
        2. 再认状态提示元素 ``ReadyToCaptionTextBlock``；
        3. **只有前两者都不存在时**才启用启发式回退。

        依据是这两个元素**互斥**：待机时只有状态提示，字幕一滚动状态提示就消失、
        字幕元素出现。所以「两者都没有」才是「AutomationId 被改过」的可靠信号；
        反过来先跑启发式，就会在待机时把状态提示误认成字幕。
        """
        if self._text_element is None:
            for automation_id in CAPTION_AUTOMATION_IDS:
                element = self._find_by_automation_id(automation_id)
                if element is not None:
                    self._text_element = element
                    self._log(f"已定位字幕元素 aid={automation_id!r}")
                    break

        if self._status_element is None:
            for automation_id in STATUS_AUTOMATION_IDS:
                element = self._find_by_automation_id(automation_id)
                if element is not None:
                    self._status_element = element
                    break

        if self._text_element is None and self._status_element is None:
            fallback = self._guess_text_element()
            if fallback is not None:
                self._text_element = fallback
                self._log("未找到 CaptionsTextBlock，已回退到启发式文本元素")

    def _guess_text_element(self):
        """启发式：窗口内名字最长的非图标、非界面提示文本块，通常就是字幕。"""
        if self._window is None:
            return None
        best = None
        best_length = 0
        for control in self._walk(self._window, max_depth=12):
            try:
                if control.ControlTypeName != "TextControl":
                    continue
                if (control.AutomationId or "") in NON_CAPTION_AUTOMATION_IDS:
                    continue
                name = (control.Name or "").strip()
            except Exception:
                continue
            if not name or _looks_like_icon(name):
                continue
            if len(name) > best_length:
                best, best_length = control, len(name)
        return best

    @staticmethod
    def _walk(control, max_depth: int = 12, depth: int = 0):
        if depth > max_depth:
            return
        yield control
        try:
            children = control.GetChildren()
        except Exception:
            return
        for child in children:
            yield from CaptionReader._walk(child, max_depth, depth + 1)

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def read(self) -> CaptionState:
        """读取一次当前字幕状态。不会抛异常，失败时返回相应状态。"""
        if not self.ensure_thread_ready():
            return CaptionState(text="", status=STATUS_NOT_RUNNING)
        if not self._window or not self.is_running():
            self._detach()
            return self._reconnect_and_read()

        if self._text_element is None:
            self._locate_elements()

        if self._text_element is not None:
            try:
                text = self._text_element.Name or ""
                return CaptionState(text=text, status=STATUS_CAPTIONING)
            except Exception:
                # 元素失效（窗口重建、字幕滚动导致节点替换），下轮重新定位。
                # 状态元素与字幕元素相互独立，不要一起清掉。
                self._text_element = None

        status = STATUS_NO_ELEMENT
        if self._status_element is not None:
            try:
                if (self._status_element.Name or "").strip():
                    status = STATUS_IDLE
            except Exception:
                self._status_element = None
        return CaptionState(text="", status=status)

    def _reconnect_and_read(self) -> CaptionState:
        """窗口不在或已断开时的处理：必要时重新拉起实时字幕并重连。

        两点设计考虑：

        * 重连尝试有 ``reconnect_interval`` 节流，避免每 120ms 都去做一次 UIA 查找；
        * 启动实时字幕有 ``launch_cooldown`` 冷却。实时字幕启动要几秒，期间窗口
          一直不存在——没有冷却的话会每隔一次尝试就 Popen 一遍，可能拉起多个进程。
        """
        now = time.monotonic()
        if now - self._last_connect_attempt < self.reconnect_interval:
            return CaptionState(text="", status=STATUS_NOT_RUNNING)
        self._last_connect_attempt = now

        if self.is_running():
            if self.connect():
                return CaptionState(text="", status=STATUS_IDLE)
            return CaptionState(text="", status=STATUS_NOT_RUNNING)

        if self.auto_launch and (now - self._last_launch_attempt) >= self.launch_cooldown:
            self._last_launch_attempt = now
            self.launch()
            return CaptionState(text="", status=STATUS_LAUNCHING)
        return CaptionState(text="", status=STATUS_NOT_RUNNING)

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self._detach()


# --------------------------------------------------------------------------- #
DEMO_PHRASES = [
    "Economics is not just about money.",
    "It studies how people use limited resources.",
    "Every choice you make has a cost.",
    "Economists call that the opportunity cost.",
    "When you watch this video you could be doing something else.",
    "That trade-off is the heart of the subject.",
]


class ScriptedCaptionReader:
    """演示与自检用：按时间轴回放字幕，模拟实时字幕的渐进式滚动。

    行为刻意贴近真实实时字幕（实测确认）：逐词追加 → 标点定稿 → **累积**。
    注意是「累积」而不是「只保留可见的两行」——真实的 ``Name`` 返回整场会话的
    完整转录，曾经因为演示数据用了滚动窗口而与真实行为不一致，掩盖了问题。

    接口与 :class:`CaptionReader` 一致，可互换。
    """

    def __init__(self, speed: float = 1.0, loop: bool = True) -> None:
        self.poll_interval = 0.12
        self._loop = loop
        self._timeline = self._build(max(0.1, float(speed)))
        self._started = time.monotonic()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build(speed: float) -> list[tuple[float, str]]:
        """生成「累积式」时间轴：已完成的句子不断累加，和真实字幕一致。"""
        timeline: list[tuple[float, str]] = []
        clock = 0.0
        committed: list[str] = []
        for phrase in DEMO_PHRASES:
            words = phrase.split(" ")
            for index in range(1, len(words) + 1):
                current = " ".join(words[:index])
                timeline.append((clock, "\n".join(committed + [current])))
                clock += 0.24 / speed
            timeline.append((clock, "\n".join(committed + [phrase])))
            clock += 0.75 / speed
            committed.append(phrase)
            clock += 0.9 / speed
        return timeline

    # ------------------------------------------------------------------ #
    def read(self) -> CaptionState:
        elapsed = time.monotonic() - self._started
        if elapsed > self._timeline[-1][0] + 1.5:
            if not self._loop:
                return CaptionState(text="", status=STATUS_IDLE)
            self._started = time.monotonic()
            elapsed = 0.0

        text = self._timeline[0][1]
        for clock, value in self._timeline:
            if clock <= elapsed:
                text = value
            else:
                break
        return CaptionState(text=text, status=STATUS_CAPTIONING)

    def close(self) -> None:
        pass

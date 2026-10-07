#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""窗口外观的公共代码：DPI 感知、圆角、置顶。

为什么要单独一个模块
--------------------
浮窗和设置面板都需要这几件事，而它们全是 ctypes 调用——缺一个参数签名就会
在**某次运行**里以 ``OverflowError`` 的形式炸掉（句柄是指针宽度，缺省按 c_int
处理）。这类代码复制第二份就是给自己埋雷，所以集中在这里。
"""

from __future__ import annotations

import ctypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

_dpi_declared = False


def enable_dpi_awareness() -> None:
    """声明进程 DPI 感知，让界面在高分屏上不发虚。"""
    global _dpi_declared
    if _dpi_declared:
        return
    _dpi_declared = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def system_dpi() -> int:
    """取系统 DPI（96 表示 100% 缩放）。"""
    try:
        return int(user32.GetDpiForSystem())
    except Exception:
        pass
    try:
        hdc = user32.GetDC(0)
        dpi = int(gdi32.GetDeviceCaps(hdc, 90))  # LOGPIXELSY
        user32.ReleaseDC(0, hdc)
        return dpi or 96
    except Exception:
        return 96


def round_window_corners(hwnd: int, width: int, height: int, radius: int) -> None:
    """把窗口裁成圆角。Tk 的控件只能是矩形，只能靠窗口区域实现。"""
    if radius <= 0:
        return
    try:
        diameter = radius * 2
        region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, diameter, diameter)
        # SetWindowRgn 接管 region 所有权，成功后不要自己删
        user32.SetWindowRgn(hwnd, region, True)
    except Exception:
        pass


def keep_topmost(hwnd: int) -> None:
    try:
        user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
        )
    except Exception:
        pass


def toplevel_hwnd(widget) -> int:
    """取 Tk 控件对应的顶层窗口句柄（客户区控件的父窗口）。"""
    try:
        handle = widget.winfo_id()
        parent = user32.GetParent(handle)
        return int(parent) if parent else int(handle)
    except Exception:
        return 0


def make_tool_window(hwnd: int) -> None:
    """标记为工具窗口 + 不抢焦点 + 分层。浮窗与设置面板都用这套。"""
    try:
        current = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        target = current | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if target != current:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, target)
    except Exception:
        pass

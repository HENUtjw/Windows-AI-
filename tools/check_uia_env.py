#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""UI Automation 运行环境自检。

为什么需要这个
--------------
读取 Windows 实时字幕完全依赖 UI Automation（UIA）。UIA 的「按句柄取元素」和
「遍历子树」走的是不同的底层路径，而某些受限环境（受限令牌、容器、沙箱）会
放行前者却拒绝后者。这会导致程序看起来「连上了窗口，但一个字都读不到」。

本脚本把每条底层能力单独测一遍，并给出明确结论，避免把环境问题误判成代码问题。

用法::

    python tools/check_uia_env.py
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uia_bootstrap  # noqa: E402  (导入即生效，必须早于 comtypes.client)

if hasattr(sys.stdout, "reconfigure"):
    # 只放宽错误处理，不改编码：中文控制台下中文仍能正常显示
    sys.stdout.reconfigure(errors="replace")

OK = "  [ OK ]"
BAD = "  [FAIL]"
WARN = "  [WARN]"

results: dict[str, bool] = {}


def record(name: str, ok: bool) -> None:
    results[name] = ok


# --------------------------------------------------------------------------- #
# 1. Win32：定位窗口（不依赖 COM，最基础的能力）
# --------------------------------------------------------------------------- #
def check_win32() -> None:
    print("\n[1] Win32 窗口枚举")
    user32 = ctypes.windll.user32

    hwnd = user32.FindWindowW("Shell_TrayWnd", None)
    print(f"{OK if hwnd else BAD} FindWindowW('Shell_TrayWnd') -> hwnd={hwnd}")

    handles: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(h, _):
        handles.append(h)
        return True

    user32.EnumWindows(enum_proc(_cb), 0)
    print(f"{OK if handles else BAD} EnumWindows -> {len(handles)} 个顶层窗口")
    record("win32", bool(hwnd) and bool(handles))


# --------------------------------------------------------------------------- #
# 2. comtypes 原生 IUIAutomation
# --------------------------------------------------------------------------- #
def check_comtypes() -> None:
    print("\n[2] comtypes 原生 IUIAutomation")
    try:
        import comtypes
        import comtypes.client
        from comtypes.gen import UIAutomationClient as UIA
    except Exception as exc:  # pragma: no cover
        print(f"{BAD} 导入 comtypes / 类型库失败：{type(exc).__name__}: {exc}")
        record("comtypes", False)
        return

    try:
        comtypes.CoInitialize()
        uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        print(f"{OK} 创建 CUIAutomation 成功")
    except Exception as exc:
        print(f"{BAD} 创建 CUIAutomation 失败：{type(exc).__name__}: {exc}")
        record("comtypes", False)
        return

    # 2a. 桌面根节点的子节点 = 所有顶层窗口
    try:
        root = uia.GetRootElement()
        cond = uia.CreateTrueCondition()
        count = root.FindAll(UIA.TreeScope_Children, cond).Length
        good = count > 0
        print(f"{OK if good else BAD} 桌面根节点 FindAll(Children) -> {count} 个窗口（期望 > 0）")
    except Exception as exc:
        good = False
        print(f"{BAD} 桌面根节点遍历失败：{type(exc).__name__}: {exc}")

    # 2b. 按句柄取元素 + 向下遍历（读取字幕真正依赖的能力）
    subtree_ok = False
    hwnd = ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None)
    if hwnd:
        try:
            el = uia.ElementFromHandle(hwnd)
            if el:
                n = el.FindAll(UIA.TreeScope_Descendants, uia.CreateTrueCondition()).Length
                subtree_ok = n > 1
                print(f"{OK if subtree_ok else BAD} ElementFromHandle + FindAll(Descendants) -> {n} 个后代（期望 > 1）")
            else:
                print(f"{BAD} ElementFromHandle 返回空")
        except Exception as exc:
            print(f"{BAD} 句柄遍历失败：{type(exc).__name__}: {exc}")

    # 2c. ElementFromPoint 通常最先被权限拦截
    try:
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        pt = wintypes.POINT((r.left + r.right) // 2, (r.top + r.bottom) // 2)
        el = uia.ElementFromPoint(pt)
        print(f"{OK} ElementFromPoint -> {el.CurrentClassName if el else None}")
    except Exception as exc:
        print(f"{WARN} ElementFromPoint 被拒绝：{exc}（该项不影响读字幕）")

    record("comtypes", bool(good) and subtree_ok)


# --------------------------------------------------------------------------- #
# 3. uiautomation 高层封装
# --------------------------------------------------------------------------- #
def check_uiautomation_package() -> None:
    print("\n[3] uiautomation 包")
    try:
        import uiautomation as auto
    except Exception as exc:
        print(f"{BAD} 导入失败：{type(exc).__name__}: {exc}")
        record("uiautomation", False)
        return
    print(f"{OK} 导入成功，版本 {getattr(auto, 'VERSION', '?')}，位置 {os.path.dirname(auto.__file__)}")
    record("uiautomation", True)


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 74)
    print("UI Automation 运行环境自检")
    print("=" * 74)
    print(f"Python {sys.version.split()[0]}  ({'64' if sys.maxsize > 0xFFFFFFFF else '32'} 位)")

    check_win32()
    check_comtypes()
    check_uiautomation_package()

    print("\n" + "=" * 74)
    print("结论")
    print("=" * 74)
    if results.get("win32") and results.get("comtypes"):
        print("  [OK] 环境正常：可以按句柄定位窗口并遍历内部元素，读取实时字幕没有障碍。")
        return 0

    if results.get("win32") and not results.get("comtypes"):
        print("  [!!] 仅 Win32 可用，UI Automation 子树遍历被拒绝。")
        print()
        print("  这几乎总是**运行环境**的限制，而不是代码问题。常见原因：")
        print("    · 在受限令牌 / 沙箱 / 容器 / 服务会话中运行（应改为普通用户终端运行）")
        print("    · 以管理员身份运行的窗口与当前进程完整性级别不匹配")
        print("    · 安全软件拦截了跨进程 COM 调用")
        print()
        print("  请在一个**普通的、非沙箱的**命令行窗口里重新运行本脚本确认。")
        return 1

    print("  [!!] Win32 基础能力都不可用，环境异常。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

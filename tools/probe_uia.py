#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Windows 实时字幕 UI Automation 结构探测工具。

用途
----
证实「程序能读到 Windows 实时字幕的文字」，并定位承载字幕的元素。
这是整个翻译器的前置条件：只有确认了元素路径，后续采集才可靠。

用法
----
1. 先按 Win+Ctrl+L 打开 Windows 实时字幕，并播放一段有英文语音的视频；
2. 运行::

       python tools/probe_uia.py

3. 如果默认类名找不到窗口，先列出所有顶层窗口再指定::

       python tools/probe_uia.py --list-windows
       python tools/probe_uia.py --class-name <类名>

常用参数
--------
--list-windows   列出所有顶层窗口的类名/进程/标题后退出
--dump-only      只打印 UI 树，不进入实时监听
--scan           自动发现：监听所有元素的文本变化，用于元素改名时兜底
--seconds N      监听 N 秒后退出，0 表示直到 Ctrl+C
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# 必须在导入 uiautomation 之前：保证 comtypes 有可写的生成缓存目录
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uia_bootstrap  # noqa: E402  (导入即生效)

try:
    import uiautomation as auto
except ImportError:  # pragma: no cover
    print("缺少依赖 uiautomation。请运行:  pip install uiautomation")
    raise SystemExit(2)

if hasattr(sys.stdout, "reconfigure"):
    # 只放宽错误处理，不改编码：中文控制台下中文仍能正常显示
    sys.stdout.reconfigure(errors="replace")

DEFAULT_CLASS = "LiveCaptionsDesktopWindow"
DEFAULT_AID = "CaptionsTextBlock"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def ts() -> str:
    """带毫秒的时间戳，用于观察字幕刷新节奏。"""
    return time.strftime("%H:%M:%S") + ".%03d" % (int(time.time() * 1000) % 1000)


def safe(fn, default=""):
    """UIA 元素随时可能失效，任何属性读取都必须容错。"""
    try:
        value = fn()
        return default if value is None else value
    except Exception:
        return default


def get_children(ctrl) -> list:
    try:
        return list(ctrl.GetChildren())
    except Exception:
        return []


def brief(text, limit: int = 140) -> str:
    text = str(text).replace("\r", "\\r").replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…"


def describe(ctrl, depth: int = 0) -> str:
    pad = "  " * depth
    return (
        f"{pad}[{safe(lambda: ctrl.ControlTypeName, '?')}] "
        f"class={safe(lambda: ctrl.ClassName)!r} "
        f"aid={safe(lambda: ctrl.AutomationId)!r} "
        f"name={brief(safe(lambda: ctrl.Name))!r}"
    )


# --------------------------------------------------------------------------- #
# 查找
# --------------------------------------------------------------------------- #
def list_top_windows() -> None:
    print(f"{'类名':<42} {'PID':>7}  标题")
    print("-" * 100)
    root = auto.GetRootControl()
    for win in get_children(root):
        print(
            f"{safe(lambda: win.ClassName):<42} "
            f"{safe(lambda: win.ProcessId, 0):>7}  "
            f"{brief(safe(lambda: win.Name), 60)}"
        )


def find_window(class_name: str):
    """按类名在顶层窗口中查找目标窗口。"""
    win = auto.Control(searchDepth=1, ClassName=class_name)
    if win.Exists(2, 0.3):
        return win
    # 退路：逐个比对顶层窗口，避免某些控件类型不被 searchDepth 匹配
    for candidate in get_children(auto.GetRootControl()):
        if safe(lambda: candidate.ClassName) == class_name:
            return candidate
    return None


def find_by_automation_id(win, automation_id: str):
    """在窗口内按 AutomationId 查找元素。"""
    ctrl = auto.Control(searchFromControl=win, AutomationId=automation_id)
    return ctrl if ctrl.Exists(1, 0.2) else None


# --------------------------------------------------------------------------- #
# 显示
# --------------------------------------------------------------------------- #
def dump_tree(win, max_depth: int) -> list[str]:
    lines: list[str] = []

    def walk(ctrl, depth: int) -> None:
        if depth > max_depth:
            return
        lines.append(describe(ctrl, depth))
        for child in get_children(ctrl):
            walk(child, depth + 1)

    walk(win, 0)
    return lines


def collect_named_elements(win, max_depth: int) -> dict:
    """收集所有带文本的元素，键为 (深度, AutomationId, ControlType) 组成的稳定标识。"""
    found: dict = {}

    def walk(ctrl, depth: int) -> None:
        if depth > max_depth:
            return
        name = safe(lambda: ctrl.Name)
        if name:
            key = (
                safe(lambda: ctrl.AutomationId),
                safe(lambda: ctrl.ControlTypeName, "?"),
                depth,
            )
            found.setdefault(key, ctrl)
        for child in get_children(ctrl):
            walk(child, depth + 1)

    walk(win, 0)
    return found


# --------------------------------------------------------------------------- #
# 监听
# --------------------------------------------------------------------------- #
def watch_single(win, automation_id: str, seconds: float) -> None:
    print(f"\n仅在 AutomationId={automation_id!r} 上监听文本变化。按 Ctrl+C 停止。\n")
    target = find_by_automation_id(win, automation_id)
    if target is None:
        print(f"[!] 没有找到 AutomationId={automation_id!r} 的元素。")
        print("    该元素的 AutomationId 可能已随系统更新改名。")
        print("    请改用自动发现模式重试：  python tools/probe_uia.py --scan")
        return

    print(f"[+] 已找到目标元素：{describe(target).strip()}")
    print("-" * 100)

    last = None
    updates = 0
    started = time.time()
    try:
        while seconds <= 0 or time.time() - started < seconds:
            text = safe(lambda: target.Name)
            if text != last:
                if last is not None:
                    updates += 1
                    # 展示"追加"还是"改写"，这决定后续断句策略
                    kind = "追加" if text.startswith(last) else "改写"
                else:
                    kind = "首次"
                print(f"[{ts()}] {kind}: {brief(text)}")
                last = text
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n[停止] 用户中断。")

    if updates:
        print("-" * 100)
        print(f"[统计] 观察到 {updates} 次文本更新。")
    else:
        print("-" * 100)
        print("[提示] 没有观察到变化。请确认视频正在播放、且实时字幕源语言为英文。")


def watch_scan(win, seconds: float, max_depth: int) -> None:
    print("\n自动发现模式：对比整棵树，报告文本发生变化的元素。")
    print("请在监听期间播放一段有英文语音的视频。按 Ctrl+C 停止。\n")
    baseline = {k: (safe(lambda c=c: c.Name), c) for k, c in collect_named_elements(win, max_depth).items()}
    print(f"[+] 基线文本元素数：{len(baseline)}")
    print("-" * 100)

    seen: dict = {}
    started = time.time()
    try:
        while seconds <= 0 or time.time() - started < seconds:
            current = collect_named_elements(win, max_depth)
            for key, ctrl in current.items():
                name = safe(lambda c=ctrl: c.Name)
                old = baseline.get(key)
                if old is None or old[0] != name:
                    count = seen.get(key, 0) + 1
                    seen[key] = count
                    if count <= 3 or count % 20 == 0:
                        print(
                            f"[{ts()}] aid={key[0]!r} type={key[1]} depth={key[2]} "
                            f"变化#{count}: {brief(name)}"
                        )
            baseline = {k: (safe(lambda c=c: c.Name), c) for k, c in current.items()}
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[停止] 用户中断。")

    print("-" * 100)
    if not seen:
        print("[提示] 没有观察到任何文本变化。请确认实时字幕正在实时显示语音内容。")
        return
    print("变化最频繁的元素（候选字幕元素）：")
    for key, count in sorted(seen.items(), key=lambda kv: -kv[1])[:5]:
        print(f"  {count:>5} 次  AutomationId={key[0]!r}  type={key[1]}  depth={key[2]}")


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="探测 Windows 实时字幕的 UI Automation 结构",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--class-name", default=DEFAULT_CLASS, help=f"目标窗口类名（默认 {DEFAULT_CLASS}）")
    parser.add_argument("--automation-id", default=DEFAULT_AID, help=f"字幕文本元素 ID（默认 {DEFAULT_AID}）")
    parser.add_argument("--seconds", type=float, default=0, help="监听时长（秒），0 表示直到 Ctrl+C")
    parser.add_argument("--max-depth", type=int, default=8, help="遍历 UI 树的最大深度")
    parser.add_argument("--list-windows", action="store_true", help="列出所有顶层窗口后退出")
    parser.add_argument("--dump-only", action="store_true", help="只打印 UI 树，不监听")
    parser.add_argument("--scan", action="store_true", help="自动发现模式：报告所有文本变化")
    args = parser.parse_args()

    if args.list_windows:
        list_top_windows()
        return 0

    print(f"[*] 查找窗口类名 {args.class_name!r} …")
    win = find_window(args.class_name)
    if win is None:
        print(f"[X] 没有找到类名为 {args.class_name!r} 的窗口。\n")
        print("请依次检查：")
        print("  1. 是否已按 Win+Ctrl+L 打开 Windows 实时字幕；")
        print("  2. 用下面的命令确认实际窗口类名：")
        print("         python tools/probe_uia.py --list-windows")
        return 1

    print(f"[+] 找到窗口：{describe(win)}")

    print("\n=== UI Automation 树 ===")
    for line in dump_tree(win, args.max_depth):
        print(line)

    if args.dump_only:
        return 0

    if args.scan:
        watch_scan(win, args.seconds, args.max_depth)
    else:
        watch_single(win, args.automation_id, args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

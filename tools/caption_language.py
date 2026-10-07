#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""查看 Windows 实时字幕的识别语言，以及全部可选语言。

为什么需要
----------
「浮窗一直显示等待实时字幕」最常见的原因不是程序坏了，而是**实时字幕的识别语言
不是 English**。用中文模型去识别英文语音只能得到乱码，翻译自然无从谈起。
而实时字幕的语言设置藏得比较深，本工具直接把它读出来。

注意：语言的**下拉列表长这样**——列出全部受支持的语言，设备上已下载的用粗体标出，
当前使用的就是下面报出来的那一个。UIA 在下拉未展开时只会暴露「当前项」，
所以本工具会自动展开再枚举，否则会误判为「只有一种语言」。

运行::

    python tools/caption_language.py

切换语言需要手动操作（会触发语言包下载，不适合脚本代劳）：

    实时字幕窗口 ⚙️ → 更改语言 → 选「英语(美国)」 → 继续 → 按提示下载
"""

from __future__ import annotations

import os
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from src.caption_reader import CaptionReader  # noqa: E402  (内部会先准备好 comtypes 缓存)

import uiautomation as auto  # noqa: E402

SETTINGS_BUTTON = "SettingsButton"
CHANGE_LANGUAGE_ITEM = "ChangeLanguageMenuFlyoutItem"
LANGUAGE_COMBO = "SpeechModelDropDown"
READY_TEXT_BLOCK = "ReadyToCaptionTextBlock"

# 「已准备好在 简体中文(中国大陆) 中显示实时字幕」
_READY_PATTERN = re.compile(r"在\s*(.+?)\s*中显示实时字幕")

ENGLISH_HINTS = ("英语", "English")


def open_language_panel(reader: CaptionReader, log=print) -> bool:
    """点开「设置 → 更改语言」，返回是否成功。"""
    button = auto.ButtonControl(searchFromControl=reader._window, AutomationId=SETTINGS_BUTTON)
    if not button.Exists(2.0, 0.2):
        log("    找不到设置按钮（实时字幕窗口结构可能已变更）")
        return False
    button.GetInvokePattern().Invoke()
    time.sleep(2.0)

    item = auto.MenuItemControl(
        searchFromControl=reader._window, AutomationId=CHANGE_LANGUAGE_ITEM
    )
    if not item.Exists(2.0, 0.2):
        log("    找不到「更改语言」菜单项")
        return False
    item.GetInvokePattern().Invoke()
    time.sleep(2.5)
    return True


def current_language_from_ready_text(reader: CaptionReader) -> str | None:
    """待机时窗口上会写「已准备好在 X 中显示实时字幕」，直接解析最可靠。"""
    block = auto.Control(searchFromControl=reader._window, AutomationId=READY_TEXT_BLOCK)
    if not block.Exists(1.0, 0.2):
        return None
    try:
        text = block.Name or ""
    except Exception:
        return None
    match = _READY_PATTERN.search(text)
    return match.group(1) if match else None


def list_languages(reader: CaptionReader) -> tuple[str | None, list[str]]:
    """展开语言下拉，返回 (当前语言, 全部语言)。"""
    combo = auto.ComboBoxControl(searchFromControl=reader._window, AutomationId=LANGUAGE_COMBO)
    if not combo.Exists(2.0, 0.2):
        return None, []

    # 展开前先记下当前项：未展开时 UIA 通常只暴露选中项
    current = None
    try:
        collapsed = [
            child.Name.strip()
            for child in combo.GetChildren()
            if child.ControlTypeName == "ListItemControl" and (child.Name or "").strip()
        ]
        if len(collapsed) == 1:
            current = collapsed[0]
    except Exception:
        pass

    try:
        combo.GetExpandCollapsePattern().Expand()
    except Exception:
        try:
            combo.Click()
        except Exception:
            pass
    time.sleep(1.5)

    names: list[str] = []
    for child in combo.GetChildren():
        try:
            if child.ControlTypeName != "ListItemControl":
                continue
            name = (child.Name or "").strip()
            if not name or name in names:
                continue
            names.append(name)
            if current is None:
                try:
                    if child.GetSelectionItemPattern().IsSelected:
                        current = name
                except Exception:
                    pass
        except Exception:
            continue
    return current, names


def main() -> int:
    print("=" * 70)
    print("Windows 实时字幕 · 识别语言检查")
    print("=" * 70)

    reader = CaptionReader(auto_launch=True, log=lambda m: print(f"  · {m}"))
    state = None
    for _ in range(40):
        state = reader.read()
        if state.status in ("idle", "captioning", "no-element"):
            break
        time.sleep(0.5)

    print(f"\n实时字幕状态：{state.status}")

    ready_language = current_language_from_ready_text(reader)
    if ready_language:
        print(f"当前识别语言：{ready_language}")
    elif state.status == "captioning":
        print("当前识别语言：字幕正在滚动，无法从界面直接读出（请先暂停播放）")

    print("\n正在展开语言列表…")
    if not open_language_panel(reader):
        print("  无法打开语言面板，请手动操作：实时字幕窗口的「设置」→ 更改语言")
        reader.close()
        return 1

    current, languages = list_languages(reader)
    if current and not ready_language:
        print(f"当前识别语言：{current}")

    if not languages:
        print("  没有读到语言列表（UI 结构可能已变更）")
        reader.close()
        return 1

    print(f"\n可选语言共 {len(languages)} 种：")
    english = [name for name in languages if any(h in name for h in ENGLISH_HINTS)]
    for name in languages:
        marks = []
        if name == current:
            marks.append("← 当前")
        if name in english:
            marks.append("英文")
        print(f"    {name}" + (f"   [{' / '.join(marks)}]" if marks else ""))

    print("\n" + "-" * 70)
    if english:
        print(f"检测到 {len(english)} 种英文模型，例如：{english[0]}")
        if current and any(h in current for h in ENGLISH_HINTS):
            print("当前已经在用英文模型，可以正常识别英文视频。")
        else:
            print("要识别英文视频，请手动切换：")
            print("    实时字幕窗口的「设置」→ 更改语言 → 选「英语(美国)」 → 继续 → 按提示下载语言包")
            print("    （首次使用需要下载语音识别文件；已下载的语言在下拉里显示为粗体）")
    else:
        print("没有检测到英文模型，请先在语言下拉里添加英语。")

    print("\n提示：切换语言后建议重新打开实时字幕，再启动本翻译程序。")

    # 收尾：关掉展开的下拉与设置面板，别把界面留在半开状态
    try:
        combo = auto.ComboBoxControl(searchFromControl=reader._window, AutomationId=LANGUAGE_COMBO)
        if combo.Exists(0.5, 0.1):
            combo.GetExpandCollapsePattern().Collapse()
    except Exception:
        pass
    try:
        auto.SendKeys("{Esc}")
    except Exception:
        pass

    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

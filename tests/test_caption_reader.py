#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""采集器离线测试：用假控件模拟实时字幕的 UI 树。

为什么必须测这个
----------------
实时字幕的 UI 树是**动态**的，两种状态互斥：

* 待机：只有 ``ReadyToCaptionTextBlock``（内容是「已准备好在…中显示实时字幕」）
* 字幕滚动中：``ReadyToCaptionTextBlock`` 消失，换成 ``CaptionsTextBlock``

曾经存在的缺陷：启发式回退先于状态元素查找执行，导致待机时把
`ReadyToCaptionTextBlock` 的界面提示误当成字幕送进翻译。
本文件把正确的查找优先级固定下来，避免以后又改回去。

用假控件的好处：不需要真的开实时字幕、不需要英文语音、不依赖系统版本。

运行::

    python tests/test_caption_reader.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from src.caption_reader import (  # noqa: E402
    STATUS_CAPTIONING,
    STATUS_IDLE,
    CaptionReader,
    ScriptedCaptionReader,
    _looks_like_icon,
    live_captions_exe,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}   {detail}")
        FAILURES.append(name)


# --------------------------------------------------------------------------- #
class FakeControl:
    """模仿 uiautomation 的 Control，只提供采集器实际用到的那几个属性。"""

    def __init__(
        self,
        name: str = "",
        automation_id: str = "",
        control_type: str = "TextControl",
        class_name: str = "TextBlock",
        children: tuple = (),
    ) -> None:
        self.Name = name
        self.AutomationId = automation_id
        self.ControlTypeName = control_type
        self.ClassName = class_name
        self._children = list(children)

    def GetChildren(self) -> list:
        return list(self._children)


def build_window(*children) -> FakeControl:
    """还原实测到的窗口结构。"""
    return FakeControl(
        name="实时辅助字幕",
        control_type="WindowControl",
        class_name="LiveCaptionsDesktopWindow",
        children=children,
    )


READY_TEXT = "已准备好在 简体中文(中国大陆) 中显示实时字幕"
CAPTION_TEXT = "Economics is not just about money."


def make_button(automation_id: str, glyph: str, label: str = "") -> FakeControl:
    return FakeControl(
        name=label,
        automation_id=automation_id,
        control_type="ButtonControl",
        class_name="Button",
        children=(FakeControl(name=glyph),),
    )


def offline_reader(elements: dict) -> CaptionReader:
    """构造一个不接触真实系统的采集器。"""
    reader = CaptionReader(auto_launch=False)
    reader._hwnd = 12345
    reader.is_running = lambda: True  # type: ignore[assignment]
    reader._find_by_automation_id = lambda aid: elements.get(aid)  # type: ignore[assignment]
    return reader


# --------------------------------------------------------------------------- #
def test_helpers() -> None:
    print("\n[工具函数]")
    check("图标字符被识别", _looks_like_icon("\ue713") is True)
    check("普通文本不算图标", _looks_like_icon("设置") is False)
    check("空字符串不算图标", _looks_like_icon("") is False)
    exe = live_captions_exe()
    check("实时字幕程序路径正确", exe.lower().endswith("system32\\livecaptions.exe"), exe)
    check("路径基于 WINDIR", os.path.dirname(os.path.dirname(exe)).lower().endswith("windows"), exe)


def test_idle_state_does_not_capture_status_text() -> None:
    """核心回归：待机时绝不能把界面提示当成字幕。"""
    print("\n[待机状态：界面提示不得被当成字幕]")
    ready = FakeControl(name=READY_TEXT, automation_id="ReadyToCaptionTextBlock")
    window = build_window(
        ready,
        make_button("SettingsButton", "\ue713", "设置"),
        make_button("CloseButton", "\ue711", "关闭实时辅助字幕窗口"),
    )
    reader = offline_reader({"ReadyToCaptionTextBlock": ready})
    reader._window = window

    reader._locate_elements()
    check("字幕元素应为空", reader._text_element is None, repr(getattr(reader._text_element, "Name", None)))
    check("状态提示元素被正确识别", reader._status_element is ready)

    state = reader.read()
    check("状态为 idle", state.status == STATUS_IDLE, state.status)
    check("文本为空（不误报字幕）", state.text == "", repr(state.text))

    # 连续多次读取都必须稳定，不能第二次才「发作」
    repeats = [reader.read() for _ in range(5)]
    check(
        "连续读取始终不误报",
        all(s.status == STATUS_IDLE and s.text == "" for s in repeats),
        str([(s.status, s.text) for s in repeats]),
    )


def test_captioning_state() -> None:
    print("\n[字幕滚动中]")
    caption = FakeControl(name=CAPTION_TEXT, automation_id="CaptionsTextBlock")
    window = build_window(
        caption,
        make_button("SettingsButton", "\ue713", "设置"),
        make_button("CloseButton", "\ue711", "关闭实时辅助字幕窗口"),
    )
    reader = offline_reader({"CaptionsTextBlock": caption})
    reader._window = window

    state = reader.read()
    check("状态为 captioning", state.status == STATUS_CAPTIONING, state.status)
    check("读到字幕文本", state.text == CAPTION_TEXT, repr(state.text))
    check("has_text 为真", state.has_text is True)

    caption.Name = "It studies how people use limited resources."
    check("文本变化能被读到", reader.read().text == caption.Name)


def test_heuristic_fallback_when_id_renamed() -> None:
    """状态提示消失、字幕元素被改名时，启发式必须接住。"""
    print("\n[AutomationId 被改名时的启发式回退]")
    renamed = FakeControl(name=CAPTION_TEXT, automation_id="BrandNewCaptionBlock")
    window = build_window(
        renamed,
        make_button("SettingsButton", "\ue713", "设置"),
        make_button("CloseButton", "\ue711", "关闭实时辅助字幕窗口"),
    )
    reader = offline_reader({})  # 已知 ID 全部找不到
    reader._window = window

    state = reader.read()
    check("启发式选中了改名后的字幕元素", reader._text_element is renamed)
    check("读出正确文本", state.text == CAPTION_TEXT, repr(state.text))
    check("状态为 captioning", state.status == STATUS_CAPTIONING, state.status)


def test_heuristic_excludes_ui_elements() -> None:
    print("\n[启发式排除界面元素]")
    ready = FakeControl(name=READY_TEXT, automation_id="ReadyToCaptionTextBlock")
    window = build_window(
        ready,
        make_button("SettingsButton", "\ue713", "设置"),
        make_button("CloseButton", "\ue711", "关闭实时辅助字幕窗口"),
    )
    reader = offline_reader({})
    reader._window = window
    check("没有可当字幕的元素时返回 None", reader._guess_text_element() is None)

    # 若状态提示被改名，仍应被 AutomationId 黑名单挡住
    renamed_status = FakeControl(name=READY_TEXT, automation_id="ReadyToCaptionTextBlock")
    window2 = build_window(renamed_status)
    reader2 = offline_reader({})
    reader2._window = window2
    check("状态提示不在启发式候选内", reader2._guess_text_element() is None)

    # 图标文字与按钮标签也不该被选中
    glyph = FakeControl(name="\ue713")
    button = make_button("SomeButton", "\ue711", "设置")
    window3 = build_window(glyph, button)
    reader3 = offline_reader({})
    reader3._window = window3
    check("图标与按钮标签被排除", reader3._guess_text_element() is None)


def test_com_initialized_in_worker_thread() -> None:
    """回归：采集循环跑在工作线程里，必须在该线程初始化 COM。

    漏掉这一步**不会抛异常**，只会静默地一条字幕都读不到。这个 bug 曾经让真实
    运行时的输出是 0 句，而所有主线程测试都正常（因为主线程早就初始化过 COM）。
    """
    print("\n[工作线程的 COM 初始化]")
    import threading

    from src.caption_reader import _thread_state

    reader = CaptionReader(auto_launch=False)
    reader.is_running = lambda: False  # type: ignore[assignment]
    outcome: dict = {}

    def worker() -> None:
        outcome["before"] = bool(getattr(_thread_state, "com_ready", False))
        reader.read()
        outcome["after"] = bool(getattr(_thread_state, "com_ready", False))

    thread = threading.Thread(target=worker, name="reader-sim")
    thread.start()
    thread.join(timeout=15)

    check("新线程起始状态为「未初始化」", outcome.get("before") is False, str(outcome))
    check("调用 read() 后该线程完成 COM 初始化", outcome.get("after") is True, str(outcome))
    check("read() 返回合法状态而不抛异常", isinstance(reader.read(), object))


def test_scripted_reader() -> None:
    print("\n[脚本化字幕源]")
    reader = ScriptedCaptionReader(speed=1.0, loop=False)
    timeline = ScriptedCaptionReader._build(1.0)
    check("时间轴非空", len(timeline) > 10, str(len(timeline)))
    check("时间轴按时间递增", all(
        timeline[i][0] <= timeline[i + 1][0] for i in range(len(timeline) - 1)
    ))
    check("存在多行滚动内容", any("\n" in text for _, text in timeline))

    state = reader.read()
    check("返回 captioning 状态", state.status == STATUS_CAPTIONING, state.status)
    check("有文本内容", bool(state.text), repr(state.text))
    check("poll_interval 合理", 0 < reader.poll_interval < 1, str(reader.poll_interval))

    # speed 越大时间轴越短
    fast = ScriptedCaptionReader._build(4.0)
    check("加速后时间轴变短", fast[-1][0] < timeline[-1][0], f"{fast[-1][0]} vs {timeline[-1][0]}")


def test_status_text_mapping() -> None:
    print("\n[状态文案]")
    from src.caption_reader import STATUS_TEXT

    for key in (STATUS_IDLE, STATUS_CAPTIONING):
        check(f"{key} 有中文说明", bool(STATUS_TEXT.get(key)), str(STATUS_TEXT.get(key)))


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 74)
    print("采集器离线测试（假控件模拟 UI 树）")
    print("=" * 74)

    test_helpers()
    test_idle_state_does_not_capture_status_text()
    test_captioning_state()
    test_heuristic_fallback_when_id_renamed()
    test_heuristic_excludes_ui_elements()
    test_com_initialized_in_worker_thread()
    test_scripted_reader()
    test_status_text_mapping()

    print("\n" + "=" * 74)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for name in FAILURES:
            print(f"  · {name}")
        return 1
    print("全部通过 (ALL PASS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""首次配置流程的测试：没有 API Key 时要用图形窗口，而不是终端提示。

运行::

    python tests/test_first_run.py

不需要网络、不需要真实弹窗（对弹窗函数做了替身）。
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

import main as app_main  # noqa: E402
from src.config import Config  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}   {detail}")
        FAILURES.append(name)


class _Route:
    """替身：记录走了哪条路，并按脚本返回结果。"""

    def __init__(self, gui_result: str | None) -> None:
        self.gui_result = gui_result
        self.calls: list[str] = []

    def gui(self, _cfg, first_run: bool = True):
        # 参数名必须与真实函数一致：调用方是用 first_run= 关键字传的
        self.calls.append("gui")
        return self.gui_result

    def terminal(self, _cfg):
        self.calls.append("terminal")
        return "sk-from-terminal"


def _with_route(gui_result, use_gui: bool, first_run: bool = True):
    route = _Route(gui_result)
    original_gui = app_main._prompt_for_api_key_gui
    original_terminal = app_main._prompt_for_api_key
    app_main._prompt_for_api_key_gui = route.gui
    app_main._prompt_for_api_key = route.terminal
    try:
        key = app_main._ask_for_api_key(Config(), use_gui=use_gui, first_run=first_run)
    finally:
        app_main._prompt_for_api_key_gui = original_gui
        app_main._prompt_for_api_key = original_terminal
    return key, route.calls


# ---------------------------------------------------------------- #
def test_gui_is_preferred() -> None:
    """图形模式下，填好了就用窗口的结果，绝不碰终端。"""
    key, calls = _with_route("sk-from-window", use_gui=True)
    check("走图形窗口", calls == ["gui"], calls)
    check("返回窗口里填的 Key", key == "sk-from-window", key)


def test_cancel_does_not_fall_back_to_terminal() -> None:
    """用户在窗口里主动取消后，不该再弹终端提示追问。

    "取消" 和 "没有图形界面" 必须区分开：前者是用户的明确意愿，
    后者才需要回退。
    """
    key, calls = _with_route("", use_gui=True)
    check("只调用了图形窗口", calls == ["gui"], calls)
    check("返回空串表示放弃", key == "", repr(key))


def test_unavailable_gui_falls_back() -> None:
    """图形界面不可用（返回 None）时才回退到命令行。"""
    key, calls = _with_route(None, use_gui=True)
    check("先试图形、再回退命令行", calls == ["gui", "terminal"], calls)
    check("返回命令行输入的值", key == "sk-from-terminal", key)


def test_console_mode_never_opens_window() -> None:
    """--console（终端模式）下不应弹窗，直接用命令行提示。"""
    key, calls = _with_route("sk-should-not-be-used", use_gui=False)
    check("只走命令行", calls == ["terminal"], calls)
    check("返回命令行输入的值", key == "sk-from-terminal", key)


def test_dialog_entry_exists() -> None:
    """图形入口确实存在且可导入（不实际弹窗）。"""
    try:
        from src.settings_dialog import prompt_for_api_key, show_message

        check("prompt_for_api_key 可导入", callable(prompt_for_api_key))
        check("show_message 可导入", callable(show_message))
    except Exception as exc:  # noqa: BLE001
        check("图形入口可导入", False, repr(exc))


def test_main_uses_gui_unless_console() -> None:
    """main() 的调用点：只有 --console 才关掉图形窗口。"""
    parser_build = app_main.build_parser()
    args_gui = parser_build.parse_args([])
    args_console = parser_build.parse_args(["--console"])
    check("默认不是 console 模式", not args_gui.console)
    check("--console 才是 console 模式", args_console.console)


def test_save_values_merges_dotted_paths() -> None:
    """设置面板靠 save_values 批量写回：只改指定项，其它配置必须原样保留。"""
    import json

    from src.config import save_values

    path = os.path.join(_ROOT, "_tmp_config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "deepseek": {"api_key": "sk-old", "model": "deepseek-flash"},
                "overlay": {"font_size": 26},
            },
            handle,
        )
    try:
        save_values(
            path,
            {
                "overlay.font_size": 21,
                "overlay.padding": 26,
                "cache.enabled": False,
                "deepseek.model": "deepseek-v4-pro",
            },
        )
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        check("覆盖已有项", data["overlay"]["font_size"] == 21, data)
        check("新建中间层", data["overlay"]["padding"] == 26, data)
        check("新建顶层段", data["cache"]["enabled"] is False, data)
        check("保留未提及的项", data["deepseek"]["api_key"] == "sk-old", data)
        check("跨段覆盖", data["deepseek"]["model"] == "deepseek-v4-pro", data)
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_settings_dialog_builds() -> None:
    """设置面板能真正构建出来，三页都能切换。

    这条测试的价值在于：界面构造的低级错误（比如给 Label 传元组 ``pady``、
    按错误的父级去筛选要保留的控件）**只有真建一次窗口才会暴露**，
    静态检查和逻辑测试都抓不到。
    """
    import tkinter as tk

    from src.settings_dialog import open_settings_dialog

    root = tk.Tk()
    root.withdraw()
    try:
        dialog = open_settings_dialog(root, Config(), on_saved=lambda _k, _m: None)
        root.update()
        check("设置面板能创建", dialog.winfo_exists() == 1)
        for name in ("conn", "look", "adv"):
            dialog.tabs.select(name)
            root.update()
            check(f"能切到分页 {name}", dialog.tabs._current == name)
        dialog.destroy()
        root.update()
        check("设置面板能销毁", dialog.winfo_exists() == 0)
    except Exception as exc:  # noqa: BLE001
        check("设置面板构建", False, repr(exc))
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_simple_mode_keeps_key_and_model() -> None:
    """首次配置模式：只留 API Key 与模型两行，且两行都真的还在。

    这里踩过坑：``row()`` 返回的是右列容器，而它在页面里的层级更深，
    按它去筛选「要保留哪些行」会把整行连同输入框一起删掉。
    """
    import tkinter as tk

    from src.settings_dialog import open_settings_dialog

    root = tk.Tk()
    root.withdraw()
    try:
        dialog = open_settings_dialog(
            root, Config(), on_saved=lambda _k, _m: None, simple=True,
            title="首次配置", subtitle="测试",
        )
        root.update()
        check("首次配置面板能创建", dialog.winfo_exists() == 1)
        # 输入框必须还在（被误删的话这里会抛 TclError）
        entries = _find_widgets(dialog, tk.Entry)
        check("API Key 输入框仍在", len(entries) >= 1, len(entries))
        dialog.destroy()
    except Exception as exc:  # noqa: BLE001
        check("首次配置面板构建", False, repr(exc))
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _find_widgets(parent, kind):
    found = []
    try:
        children = parent.winfo_children()
    except Exception:
        return found
    for child in children:
        if isinstance(child, kind):
            found.append(child)
        found.extend(_find_widgets(child, kind))
    return found


def test_overlay_hot_apply() -> None:
    """设置面板存完后浮窗要**当场**变化，而不是等下次启动。

    这是「外观类改动立即生效」这句承诺的实现验证：字号、显示句数、
    以及重建之后内容不能丢。
    """
    from src.overlay import Overlay

    cfg = Config()
    cfg.set("overlay.font_size", 20)
    cfg.set("overlay.enabled", True)
    overlay = Overlay(cfg)
    try:
        before = str(overlay._rows[0][1].cget("font"))
        cfg.set("overlay.font_size", 31)
        overlay.apply_settings()
        after = str(overlay._rows[0][1].cget("font"))
        check("字号热应用生效", "31" in after and before != after, f"{before} -> {after}")

        cfg.set("overlay.max_sentences", 3)
        overlay.apply_settings()
        check("显示句数热应用生效", len(overlay._rows) == 3, len(overlay._rows))

        overlay._render([("Hello there", "你好")], "")
        cfg.set("overlay.font_size", 18)
        overlay.apply_settings()
        check(
            "重建后已显示的内容不丢",
            overlay._rows[0][1].cget("text") == "你好",
            overlay._rows[0][1].cget("text"),
        )
    except Exception as exc:  # noqa: BLE001
        check("浮窗热应用", False, repr(exc))
    finally:
        try:
            overlay.destroy()
        except Exception:
            pass


def test_hot_apply_does_not_destroy_settings_dialog() -> None:
    """回归：浮窗热应用**不能**把设置面板一起销毁。

    实测（用户真实运行）：保存时 ``on_apply`` → ``overlay.apply_settings()``
    会遍历 ``root.winfo_children()`` 全部销毁，而设置面板正是这个 root 下的一个
    Toplevel —— 于是对话框在点「保存」的瞬间消失，紧接着
    ``status.configure()`` 抛 ``TclError: invalid command name "...!label"``。
    """
    from src.overlay import Overlay
    from src.settings_dialog import open_settings_dialog

    cfg = Config()
    overlay = Overlay(cfg)
    dialog = None
    try:
        dialog = open_settings_dialog(overlay._root, cfg, on_saved=lambda _k, _m: None)
        overlay._root.update()
        check("设置面板已创建", dialog.winfo_exists() == 1)

        cfg.set("overlay.font_size", 24)
        overlay.apply_settings()
        overlay._root.update()
        check(
            "热应用后设置面板仍然存在",
            dialog.winfo_exists() == 1,
            "对话框被浮窗的重建一起销毁了",
        )
        # 面板里的控件也必须还能用（这正是原报错崩掉的地方）
        alive = _find_widgets(dialog, __import__("tkinter").Label)
        check("面板内控件仍可用", len(alive) > 0, len(alive))
    except Exception as exc:  # noqa: BLE001
        check("热应用不影响设置面板", False, repr(exc))
    finally:
        for target in (dialog, overlay):
            try:
                if target is not None:
                    target.destroy()
            except Exception:
                pass


# ---------------------------------------------------------------- #
def main() -> int:
    test_gui_is_preferred()
    test_cancel_does_not_fall_back_to_terminal()
    test_unavailable_gui_falls_back()
    test_console_mode_never_opens_window()
    test_dialog_entry_exists()
    test_main_uses_gui_unless_console()
    test_save_values_merges_dotted_paths()
    test_settings_dialog_builds()
    test_simple_mode_keeps_key_and_model()
    test_overlay_hot_apply()
    test_hot_apply_does_not_destroy_settings_dialog()
    print()
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("全部通过 (ALL PASS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

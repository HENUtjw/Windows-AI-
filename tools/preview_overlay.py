#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""浮窗显示预览：不播视频就能渲染各种状态并截图，用来评估/调整外观。

为什么需要
----------
字幕的显示效果只有在「真实内容 + 真实状态」下才能判断：译文还没到时的占位、
流式输出到一半、超长句换行、还没开始识别……这些状态各自长什么样，光看代码
是看不出来的。本工具把它们逐个渲染出来。

用法::

    python tools/preview_overlay.py --all            # 渲染全部场景到 _preview/ 目录
    python tools/preview_overlay.py --scene pending  # 只渲染某个场景
    python tools/preview_overlay.py --scene long --out long.png
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from src.config import Config  # noqa: E402
from src.overlay import Overlay  # noqa: E402

EN1 = "Choice you make has a cost."
ZH1 = "你做的每个选择都有代价。"
EN2 = "And economists call that the opportunity cost."
ZH2 = "经济学家称之为机会成本。"
EN3 = "When you watch this video, you could be doing something else instead."
ZH3 = "你看这个视频的时候，本来可以做别的事。"

SCENES: dict[str, dict] = {
    "normal": {
        "desc": "正常：两句都已译完",
        "rows": [(EN1, ZH1), (EN2, ZH2)],
        "status": "",
    },
    "partial": {
        "desc": "流式输出中：最新一句只有半句译文",
        "rows": [(EN2, ZH2), (EN3, "你看这个视频的时候，")],
        "status": "",
    },
    "pending": {
        "desc": "刚提交：最新一句译文还没到（占位状态）",
        "rows": [(EN2, ZH2), (EN3, "")],
        "status": "",
    },
    "long": {
        "desc": "超长句：译文换行占三行",
        "rows": [
            (EN2, ZH2),
            (
                "That trade off is the heart of the subject we are going to study "
                "together, and it will keep coming back throughout the course.",
                "这种取舍正是我们将要一起学习的这门学科的核心，而且它会在整个课程中反复出现。",
            ),
        ],
        "status": "",
    },
    "waiting": {
        "desc": "还没识别到内容（只有状态行）",
        "rows": [],
        "status": "等待实时字幕…",
    },
    "error": {
        "desc": "翻译失败时的提示",
        "rows": [(EN2, ZH2), (EN3, "⚠ DeepSeek 接口返回 HTTP 429。请求过于频繁，已被限流。")],
        "status": "",
    },
}


def render(scene_name: str, out_path: str, config_path: str | None) -> int:
    scene = SCENES[scene_name]
    cfg = Config.load(config_path)
    cfg.set("captions.log_raw_text", False)
    cfg.set("overlay.position", "top-center")  # 预览时放顶部，避免被任务栏挡住

    # 传入空回调，让预览里也出现 ⚙ / ✕ 两个按钮（与真实程序一致）
    overlay = Overlay(cfg, on_settings=lambda: None, on_close=lambda: None)
    outcome: dict = {}

    def worker() -> None:
        try:
            time.sleep(0.8)
            overlay.post(scene["rows"], scene["status"])
            time.sleep(1.2)
            from win_capture import capture_window

            outcome["size"] = capture_window(overlay._hwnd(), out_path)
        except Exception as exc:  # 预览失败也要把窗口关掉
            import traceback

            traceback.print_exc()
            outcome["error"] = repr(exc)
        finally:
            overlay.request_close()

    threading.Thread(target=worker, daemon=True).start()
    overlay.run()

    if "error" in outcome:
        return 1
    print(f"  {scene_name:<10} {scene['desc']}  ->  {out_path}  {outcome.get('size')}")
    return 0


def render_dialog(out_path: str, config_path: str | None, tab: str | None = None) -> int:
    """渲染设置面板并截图（设置界面也要看得见才好调）。"""
    import tkinter as tk

    from src.settings_dialog import open_settings_dialog

    cfg = Config.load(config_path)
    root = tk.Tk()
    root.withdraw()
    dialog = open_settings_dialog(root, cfg, on_saved=lambda _k, _m: None)
    if tab:
        try:
            dialog.tabs.select(tab)
        except Exception as exc:
            print(f"  切换分页失败（{tab}）：{exc}")
    holder: dict = {}

    def shoot() -> None:
        try:
            root.update_idletasks()
            from win_capture import capture_window

            holder["size"] = capture_window(dialog.winfo_id(), out_path)
        except Exception:
            import traceback

            traceback.print_exc()
        finally:
            root.quit()

    root.after(900, shoot)
    root.mainloop()
    try:
        root.destroy()
    except Exception:
        pass
    if "size" not in holder:
        return 1
    print(f"  {'settings':<10} 设置面板  ->  {out_path}  {holder['size']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染浮窗的各种显示状态并截图")
    parser.add_argument("--scene", choices=sorted(SCENES), help="要渲染的场景")
    parser.add_argument("--all", action="store_true", help="渲染全部场景")
    parser.add_argument("--dialog", action="store_true", help="渲染设置面板")
    parser.add_argument(
        "--tab", choices=["conn", "look", "adv"], help="渲染设置面板的哪一页（翻译/外观/字幕与缓存）"
    )
    parser.add_argument("--out", help="输出 PNG 路径（单场景时使用）")
    parser.add_argument("--outdir", default=os.path.join(_ROOT, "_preview"), help="批量输出目录")
    parser.add_argument("--config", help="配置文件路径")
    args = parser.parse_args()

    if args.all:
        os.makedirs(args.outdir, exist_ok=True)
        print(f"渲染全部场景到 {args.outdir}")
        failures = 0
        for name in SCENES:
            target = os.path.join(args.outdir, f"{name}.png")
            # 每个场景单开一个进程：Tk 在同一进程里反复创建/销毁根窗口并不可靠
            result = subprocess.run(
                [sys.executable, os.path.abspath(__file__), "--scene", name, "--out", target,
                 *(["--config", args.config] if args.config else [])],
                cwd=_ROOT,
            )
            failures += result.returncode != 0
        failures += subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--dialog",
             "--out", os.path.join(args.outdir, "settings.png"),
             *(["--config", args.config] if args.config else [])],
            cwd=_ROOT,
        ).returncode != 0
        return 1 if failures else 0

    if args.dialog:
        out = args.out or os.path.join(
            _ROOT, "_preview", f"settings{'_' + args.tab if args.tab else ''}.png"
        )
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        return render_dialog(out, args.config, args.tab)

    if not args.scene:
        parser.error("请指定 --scene 或 --all")
    out = args.out or os.path.join(_ROOT, "_preview", f"{args.scene}.png")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    return render(args.scene, out, args.config)


if __name__ == "__main__":
    raise SystemExit(main())

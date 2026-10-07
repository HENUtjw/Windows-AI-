#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""置顶字幕浮窗（仿 Windows 实时字幕的外观与交互）。

外观
----
深色玻璃面板 + 圆角 + 右上角两个图标按钮。图标用的是 Windows 自己的符号字体
（``\\ue713`` 设置、``\\ue711`` 关闭），和系统实时字幕上那两个按钮是同一套字形。

交互
----
* **整块面板都能拖动**：鼠标事件从子控件冒泡到 toplevel，在那里统一处理；
  按钮自己的绑定返回 ``"break"`` 阻断冒泡，所以点按钮不会变成拖动。
* **⚙ 打开设置面板**（填写 / 更换 API Key），**✕ 关闭**——关闭时会一并关掉
  Windows 实时字幕（由上层回调决定）。
* 拖动后的位置会记下来，下次启动还在原地。

三个必须踩过的坑
----------------
1. **高 DPI**：进程默认 DPI-unaware，Windows 会拉伸整个窗口位图，字幕发虚，
   而且 ``winfo_screenwidth()`` 返回缩放后的逻辑尺寸。所以启动前先声明 DPI 感知。
2. **扩展样式被 Tk 覆盖**：Tk 在 ``-topmost`` / ``-alpha`` 变化时会重写窗口的
   ``WS_EX_*``，把穿透样式冲掉，所以样式要随每次重排重新确认。
3. **圆角只能用窗口区域**：Tk 的 Frame 只能是矩形。这里用 ``SetWindowRgn`` +
   ``CreateRoundRectRgn`` 把窗口裁成圆角——比 ``-transparentcolor`` 与 ``-alpha``
   叠加更可靠（那两个会覆盖同一份 layered 属性，互相打架）。

跨线程注意：Tkinter 不是线程安全的，界面操作全部在主线程，外部只能调用
:meth:`Overlay.post`。
"""

from __future__ import annotations

import ctypes
import queue
import tkinter as tk
from tkinter import font as tkfont
from typing import Sequence

from .config import Config
from . import winchrome

# 扩展样式
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

# SetWindowPos
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

_STYLE_MASK = WS_EX_TRANSPARENT

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

_dpi_declared = False

# Windows 自带符号字体里的两个字形，与系统实时字幕按钮同款
GLYPH_SETTINGS = "\ue713"
GLYPH_CLOSE = "\ue711"


# DPI 感知与圆角都实现在 winchrome 里：设置面板也要用同一套，
# 这类 ctypes 代码复制第二份就是给自己埋雷（缺参数签名会 OverflowError）。
_enable_dpi_awareness = winchrome.enable_dpi_awareness
_system_dpi = winchrome.system_dpi


def icon_font_family() -> str:
    """挑一个装有 Windows 符号字形的字体（设置 / 关闭 / 复选框都用它）。

    用系统自带的符号字体而不是 Unicode 图形字符，才能和系统实时字幕长得一样，
    也不必担心中文字体里有没有对应字形。
    """
    try:
        families = set(tkfont.families())
    except Exception:
        return "Segoe MDL2 Assets"
    for name in ("Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe UI Symbol"):
        if name in families:
            return name
    return "Segoe MDL2 Assets"


class Overlay:
    """字幕浮窗。必须在主线程构造并调用 :meth:`run`。"""

    def __init__(self, cfg: Config, on_quit=None, on_settings=None, on_close=None) -> None:
        self.cfg = cfg
        self._on_quit = on_quit or (lambda: None)
        self._on_settings = on_settings
        # ✕ 的语义是「关掉整个程序」；是否连系统实时字幕一起关由上层回调决定
        self._on_close = on_close or self._on_quit

        self._queue: queue.Queue = queue.Queue()
        # 界面操作只能由主线程执行。热键线程等外部调用把命令放进这里，
        # 由主线程的定时器取出执行——绝不能从别的线程直接碰 Tk。
        self._commands: queue.Queue = queue.Queue()
        self._close_requested = False

        self._read_theme()
        self._offset_y = 0
        self._manual_pos: tuple[int, int] | None = None
        self._last_size: tuple[int, int] | None = None
        self._drag_origin: tuple[int, int, int, int] | None = None
        # 最后一次画上去的内容。热应用要重建面板，重建后靠它把内容补回去。
        self._last_payload: tuple[list[tuple[str, str]], str] | None = None

        _enable_dpi_awareness()

        self._root = tk.Tk()
        self._root.withdraw()
        self._build()
        self._root.deiconify()
        self._reposition()

    # ------------------------------------------------------------------ #
    # 构建界面
    # ------------------------------------------------------------------ #
    def _read_theme(self) -> None:
        """把配置里的配色/字号/间距读进实例属性。热应用时会再读一次。"""
        cfg = self.cfg
        self._background = str(cfg.get("overlay.background", "#1C1C1C"))
        self._border_color = str(cfg.get("overlay.border_color", "#3A3A3A"))
        self._text_color = str(cfg.get("overlay.text_color", "#FFFFFF"))
        self._source_color = str(cfg.get("overlay.source_color", "#9AA0A6"))
        self._placeholder_color = str(cfg.get("overlay.placeholder_color", "#6B7280"))
        self._icon_color = str(cfg.get("overlay.icon_color", "#C8C8C8"))
        self._icon_hover_color = str(cfg.get("overlay.icon_hover_color", "#FFFFFF"))
        self._corner_radius = max(0, int(cfg.get("overlay.corner_radius", 12)))
        self._max_rows = max(1, int(cfg.get("overlay.max_sentences", 2)))
        self._width = int(cfg.get("overlay.width", 1400))
        self._show_buttons = bool(cfg.get("overlay.show_buttons", True))
        self._click_through = bool(cfg.get("overlay.click_through", False))
        # 尺寸可能变了，强制重设圆角区域
        self._last_size = None

    def _build(self) -> None:
        root = self._root
        try:
            root.tk.call("tk", "scaling", _system_dpi() / 72.0)
        except Exception:
            pass

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", float(self.cfg.get("overlay.alpha", 0.9)))

        self._build_panel()

        # 拖动：绑在 toplevel 上，子控件事件会冒泡到这里；
        # 按钮自己的绑定返回 "break"，所以点按钮不会变成拖动。
        root.bind("<ButtonPress-1>", self._drag_start)
        root.bind("<B1-Motion>", self._drag_move)
        root.bind("<ButtonRelease-1>", self._drag_end)

    def _build_panel(self) -> None:
        """构建面板本体。热应用时会整体重建，所以单独拆出来。"""
        cfg = self.cfg
        padding = int(cfg.get("overlay.padding", 14))
        spacing = int(cfg.get("overlay.line_spacing", 6))
        group_spacing = int(cfg.get("overlay.group_spacing", 16))
        family = str(cfg.get("overlay.font_family", "Microsoft YaHei UI"))
        font_size = int(cfg.get("overlay.font_size", 26))
        source_size = int(cfg.get("overlay.source_font_size", 15))
        self._show_source = bool(cfg.get("overlay.show_source", True))
        # 文字到边框的距离。注意 wraplength 要把边框(1px)和内边距都扣掉，
        # 否则长句会顶到右边框上。
        wraplength = max(200, self._width - 2 * (padding + 1))

        self._root.configure(bg=self._border_color)

        # 1px 边框：外层用边框色，内层面板留 1px 露出边缘
        self._frame = tk.Frame(self._root, bg=self._background)
        self._frame.pack(fill="both", expand=True, padx=1, pady=1)

        # 所有内容放进这个带内边距的容器，让文字与边框之间留出距离
        self._content = tk.Frame(self._frame, bg=self._background)
        self._content.pack(fill="both", expand=True, padx=padding, pady=(max(4, padding // 3), padding))

        if self._show_buttons:
            self._build_header(family)
        else:
            tk.Frame(self._content, bg=self._background, height=max(2, spacing)).pack(fill="x")

        self._rows: list[tuple[tk.Label | None, tk.Label]] = []
        for index in range(self._max_rows):
            if self._show_source:
                source: tk.Label | None = tk.Label(
                    self._content, text="", font=(family, source_size),
                    fg=self._source_color, bg=self._background,
                    anchor="w", justify="left", wraplength=wraplength,
                )
                source.pack(fill="x", pady=(spacing + (group_spacing if index else 0), 0))
            else:
                source = None

            translation = tk.Label(
                self._content, text="", font=(family, font_size, "bold"),
                fg=self._text_color, bg=self._background,
                anchor="w", justify="left", wraplength=wraplength,
            )
            translation.pack(fill="x", pady=(0, spacing))
            self._rows.append((source, translation))

        self._status = tk.Label(
            self._content, text="等待实时字幕…", font=(family, max(10, source_size - 3)),
            fg=self._placeholder_color, bg=self._background, anchor="w",
        )
        self._status.pack(fill="x", pady=(0, 6))

    def apply_settings(self, reset_position: bool = True) -> None:
        """重新读取配置并应用（设置面板保存后调用）。

        外观相关的项**立刻生效**，不用重启：配色、字号、内边距、圆角、
        不透明度、显示句数、是否显示原文、位置。实现方式是把面板整体重建——
        因为像「同时显示几句」会改变控件数量，逐项 configure 会很难维护。
        """
        self._read_theme()
        try:
            self._root.attributes("-alpha", float(self.cfg.get("overlay.alpha", 0.9)))
        except Exception:
            pass
        # 只销毁浮窗自己的面板。
        # 不能遍历 root.winfo_children() 全部销毁：设置面板是同一个 Tk root 下的
        # 一个 Toplevel，一起销毁会在用户点「保存」的瞬间把对话框也干掉，
        # 紧接着对话框里的 status.configure() 就抛
        # TclError: invalid command name "....!label"（实测于用户真实运行）。
        panel = getattr(self, "_frame", None)
        for child in self._root.winfo_children():
            if child is panel:
                child.destroy()
        self._build_panel()
        if reset_position and str(self.cfg.get("overlay.position", "")) != "custom":
            self._manual_pos = None
        # 重建后把最后一份内容重新画上去，避免面板短暂空白
        if self._last_payload is not None:
            self._render(*self._last_payload)
        self._apply_window_style()
        self._reposition()

    def _build_header(self, _family: str) -> None:
        header = tk.Frame(self._content, bg=self._background)
        header.pack(fill="x", pady=(0, 2))

        icon_family = icon_font_family()
        size = int(self.cfg.get("overlay.icon_size", 14))

        def make_button(glyph: str, command) -> tk.Label:
            label = tk.Label(
                header, text=glyph, font=(icon_family, size),
                fg=self._icon_color, bg=self._background, padx=7, pady=2,
                cursor="hand2",
            )
            label.pack(side="right")

            def on_enter(_event) -> None:
                label.configure(fg=self._icon_hover_color)

            def on_leave(_event) -> None:
                label.configure(fg=self._icon_color)

            def on_click(_event) -> str:
                command()
                return "break"  # 别让事件冒泡成拖动

            label.bind("<Enter>", on_enter)
            label.bind("<Leave>", on_leave)
            label.bind("<Button-1>", on_click)
            return label

        # 右起：关闭、设置
        make_button(GLYPH_CLOSE, self._on_close)
        if self._on_settings is not None:
            make_button(GLYPH_SETTINGS, self._on_settings)

    # ------------------------------------------------------------------ #
    # 窗口样式、圆角与位置
    # ------------------------------------------------------------------ #
    def _hwnd(self) -> int:
        handle = self._root.winfo_id()
        parent = user32.GetParent(handle)
        return int(parent) if parent else int(handle)

    def _apply_window_style(self) -> None:
        """确保置顶 / 不抢焦点 / 穿透样式生效（Tk 可能在改属性时把它们冲掉）。"""
        hwnd = self._hwnd()
        # 面板永不抢焦点，否则点按钮会把视频播放器的键盘快捷键抢走
        desired = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if self._click_through:
            desired |= _STYLE_MASK
        try:
            current = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            target = (current & ~_STYLE_MASK) | desired
            if target != current:
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, target)
        except Exception:
            pass

    def _apply_round_corners(self, width: int, height: int) -> None:
        """把窗口裁成圆角（实现在 winchrome，与设置面板共用）。"""
        winchrome.round_window_corners(self._hwnd(), width, height, self._corner_radius)

    def _ensure_topmost(self) -> None:
        try:
            user32.SetWindowPos(
                self._hwnd(), HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE,
            )
        except Exception:
            pass

    def toggle_click_through(self) -> bool:
        self._click_through = not self._click_through
        self._apply_window_style()
        self._ensure_topmost()
        return self._click_through

    def _reposition(self) -> None:
        self._root.update_idletasks()
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()

        width = min(self._width, max(320, screen_w - 40))
        height = max(80, self._root.winfo_reqheight())

        if self._manual_pos is not None:
            x, y = self._manual_pos
        else:
            position = str(self.cfg.get("overlay.position", "bottom-center"))
            margin_x = int(self.cfg.get("overlay.margin_x", 40))
            margin_bottom = int(self.cfg.get("overlay.margin_bottom", 90))
            margin_top = int(self.cfg.get("overlay.margin_top", 60))
            if position == "custom":
                x = int(self.cfg.get("overlay.x", (screen_w - width) // 2))
                y = int(self.cfg.get("overlay.y", screen_h - height - margin_bottom))
            elif position == "top-center":
                x, y = (screen_w - width) // 2, margin_top
            elif position == "bottom-left":
                x, y = margin_x, screen_h - height - margin_bottom
            elif position == "bottom-right":
                x, y = screen_w - width - margin_x, screen_h - height - margin_bottom
            else:
                x, y = (screen_w - width) // 2, screen_h - height - margin_bottom
            y += self._offset_y
            y = max(0, min(y, max(0, screen_h - height)))

        self._root.geometry(f"{width}x{height}+{x}+{y}")

        # 圆角区域只在尺寸变化时重设，避免每次刷新都动窗口区域造成闪烁
        if self._last_size != (width, height):
            self._last_size = (width, height)
            self._apply_round_corners(width, height)

        self._apply_window_style()
        self._ensure_topmost()

    # ------------------------------------------------------------------ #
    # 拖动
    # ------------------------------------------------------------------ #
    def _drag_start(self, event) -> None:
        self._drag_origin = (event.x_root, event.y_root, self._root.winfo_x(), self._root.winfo_y())

    def _drag_move(self, event) -> None:
        if self._drag_origin is None:
            return
        base_x, base_y, win_x, win_y = self._drag_origin
        x = win_x + (event.x_root - base_x)
        y = win_y + (event.y_root - base_y)
        self._manual_pos = (x, y)
        self._root.geometry(f"+{x}+{y}")
        self._ensure_topmost()

    def _drag_end(self, _event) -> None:
        self._drag_origin = None

    @property
    def manual_position(self) -> tuple[int, int] | None:
        """用户拖动后的位置（供上层持久化）。"""
        return self._manual_pos

    def nudge(self, delta: int) -> None:
        self._offset_y += delta
        self._manual_pos = None
        self._reposition()

    def toggle_visible(self) -> None:
        if self._root.state() == "withdrawn":
            self._root.deiconify()
            self._reposition()
        else:
            self._root.withdraw()

    # ------------------------------------------------------------------ #
    # 跨线程接口
    # ------------------------------------------------------------------ #
    def post(self, rows: Sequence[tuple[str, str]], status: str = "") -> None:
        """投递要显示的内容。可在任意线程调用。"""
        self._queue.put((list(rows), status))

    def post_command(self, name: str, *args) -> None:
        """把界面操作排队给主线程执行。可在任意线程调用。

        Tk 不是线程安全的：从热键线程直接调 ``toggle_visible`` 之类的界面方法
        可能和主循环互锁。统一走这个队列。
        """
        self._commands.put((name, args))

    def _handle_command(self, name: str, args: tuple) -> None:
        if name == "nudge":
            self.nudge(int(args[0]))
        elif name == "toggle_visible":
            self.toggle_visible()
        elif name == "click_through":
            self.toggle_click_through()
        elif name == "close":
            self._close_requested = True

    # ------------------------------------------------------------------ #
    def _drain(self) -> None:
        # 关闭请求与界面命令都只在主线程执行
        while True:
            try:
                name, args = self._commands.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_command(name, args)
            except Exception:
                pass
        if self._close_requested:
            self._root.quit()
            return

        latest = None
        try:
            while True:
                latest = self._queue.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            self._render(*latest)
        self._root.after(60, self._drain)

    def _render(self, rows: list[tuple[str, str]], status: str) -> None:
        self._last_payload = (list(rows), status)
        rows = rows[-self._max_rows :]
        for index, (source_label, translation_label) in enumerate(self._rows):
            if index < len(rows):
                source_text, translation_text = rows[index]
                if source_label is not None:
                    source_label.configure(text=source_text)
                if translation_text:
                    translation_label.configure(text=translation_text, fg=self._text_color)
                else:
                    # 译文还没到：显示一个低调但看得清的占位，而不是让空白看起来像坏了
                    translation_label.configure(text="翻译中…", fg=self._placeholder_color)
            else:
                if source_label is not None:
                    source_label.configure(text="")
                translation_label.configure(text="")

        if status:
            self._status.configure(text=status)
            if not self._status.winfo_ismapped():
                self._status.pack(fill="x", pady=(0, 6))
        else:
            self._status.pack_forget()
        self._reposition()

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        self._root.after(60, self._drain)
        try:
            self._root.mainloop()
        finally:
            self._on_quit()

    def request_close(self) -> None:
        """线程安全地请求关闭浮窗（可从任意线程调用）。

        这里**不能**调 Tk 的 ``after``：Tcl 解释器不是线程安全的，从别的线程碰它
        会与主循环互锁（实测会直接卡死）。只置一个标志，由主线程的定时器去关。
        """
        self._close_requested = True

    def destroy(self) -> None:
        try:
            self._root.quit()
            self._root.destroy()
        except Exception:
            pass

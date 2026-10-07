#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""设置面板。

为什么不用系统标题栏
--------------------
系统标题栏的颜色由 Windows 主题决定，用户系统是浅色主题时，它会在深色面板
上面压出一条亮白条——非常突兀，而且改不了。所以这里用 ``overrideredirect``
去掉它，自己画一条同色的标题栏（可拖动 + 关闭按钮），并复用浮窗那套圆角处理。

视觉设计参考了现代设置界面的通行做法
------------------------------------
* **卡片分组**：一组设置放进一块比背景略亮的卡片里，而不是让控件散落在底色上。
* **开关用拨动式**（pill switch）而不是方框复选框——后者是 90 年代的观感。
* **滑块独占一行**：标题和当前值在上一行、轨道占满整行。塞在右侧会挤成一小段。
* **每项都有说明文字**，用户不必猜某个参数是干什么的。

分页与窗口边缘都自己绘制：``ttk.Notebook`` 走系统主题，在深色下会是一块浅色控件。
"""

from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from tkinter import font as tkfont

from . import winchrome
from .config import Config, save_values
from .translator import probe_api_key

MODELS = ["deepseek-flash", "deepseek-v4-pro"]
POSITIONS = ["bottom-center", "top-center", "bottom-left", "bottom-right"]
POSITION_LABELS = {
    "bottom-center": "底部居中",
    "top-center": "顶部居中",
    "bottom-left": "左下角",
    "bottom-right": "右下角",
    # 拖动过浮窗后配置里存的是 custom：它不在可选列表里（没有坐标就没意义），
    # 但必须能显示出人话，否则下拉里会赤裸裸写着 "custom"
    "custom": "自定义（你拖动过浮窗）",
}
API_KEYS_URL = "https://platform.deepseek.com/api_keys"

GLYPH_CLOSE = "\ue711"


def _icon_font() -> str:
    """挑一个装有 Windows 符号字形的字体。"""
    try:
        families = set(tkfont.families())
    except Exception:
        return "Segoe MDL2 Assets"
    for name in ("Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe UI Symbol"):
        if name in families:
            return name
    return "Segoe MDL2 Assets"


def _mask(key: str) -> str:
    """只露出头尾，避免在界面上完整暴露 Key。"""
    key = (key or "").strip()
    if not key:
        return "（尚未配置）"
    if len(key) <= 12:
        return key[:4] + "…"
    return f"{key[:7]}…{key[-4:]}"


def _tint(color: str, amount: float) -> str:
    """把颜色往白色方向调亮（链接色由主色算出来，避免出现两个互不相干的蓝）。"""
    try:
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    except (ValueError, IndexError):
        return color
    r = int(r + (255 - r) * amount)
    g = int(g + (255 - g) * amount)
    b = int(b + (255 - b) * amount)
    return f"#{r:02X}{g:02X}{b:02X}"


# 内容区宽度。窗口的自然宽度由它决定——写成 1 的话窗口会被挤成一条
# （文字疯狂换行，高度暴涨），必须给一个明确值。
CONTENT_WIDTH = 516


class _Tabs:
    """自绘深色分页条。"""

    def __init__(self, parent: tk.Misc, p: dict) -> None:
        self.p = p
        self.bar = tk.Frame(parent, bg=p["bg"])
        self.bar.pack(fill="x", padx=24, pady=(2, 0))
        self.body = tk.Frame(parent, bg=p["bg"])
        self.body.pack(fill="both", expand=True, padx=24, pady=(14, 4))
        self._items: dict[str, tuple[tk.Label, tk.Frame, tk.Frame]] = {}
        self._current = ""
        # 切页后让窗口重新适应高度（由调用方设置）
        self.on_resize = None

    def add(self, key: str, text: str) -> tk.Frame:
        p = self.p
        holder = tk.Frame(self.bar, bg=p["bg"])
        holder.pack(side="left", padx=(0, 6))
        label = tk.Label(
            holder, text=text, bg=p["bg"], fg=p["dim"],
            font=(p["family"], 10), cursor="hand2", padx=14, pady=6,
        )
        label.pack(fill="x")
        underline = tk.Frame(holder, bg=p["bg"], height=2)
        underline.pack(fill="x")
        content = tk.Frame(self.body, bg=p["bg"])
        # 用一条定宽占位条把这一页的自然宽度定住。
        # 不能靠 pack_propagate(False) 固定 body 宽度——那会**连高度一起锁死**，
        # 之后 winfo_reqheight() 就拿不到内容高度，窗口会塌成一条。
        tk.Frame(content, bg=p["bg"], width=CONTENT_WIDTH, height=1).pack()
        label.bind("<Button-1>", lambda _event, k=key: self.select(k))
        self._items[key] = (label, underline, content)
        return content

    def finish(self) -> None:
        """内容宽度由每页的占位条决定，高度随当前页自然变化。"""

    def select(self, key: str) -> None:
        p = self.p
        if key == self._current:
            return
        self._current = key
        for name, (label, underline, content) in self._items.items():
            active = name == key
            label.configure(fg=p["fg"] if active else p["dim"])
            underline.configure(bg=p["accent"] if active else p["bg"])
            if active:
                content.pack(fill="both", expand=True)
            else:
                content.pack_forget()
        if self.on_resize is not None:
            self.on_resize()


def _build_dialog(
    parent: tk.Misc,
    cfg: Config,
    *,
    on_saved=None,
    on_cache_toggle=None,
    cache_stats_provider=None,
    on_apply=None,
    title: str = "设置",
    subtitle: str | None = None,
    simple: bool = False,
) -> tk.Toplevel:
    bg = str(cfg.get("overlay.background", "#1C1C1C"))
    p = {
        "bg": bg,
        "fg": str(cfg.get("overlay.text_color", "#FFFFFF")),
        "dim": str(cfg.get("overlay.source_color", "#9AA0A6")),
        "faint": str(cfg.get("overlay.placeholder_color", "#6B7280")),
        "family": str(cfg.get("overlay.font_family", "Microsoft YaHei UI")),
        "card": "#242424",
        "field": "#1A1A1A",
        "line": "#333333",
        "accent": str(cfg.get("overlay.accent_color", "#2D6CDF")),
        "good": "#7DD3A0",
        "bad": "#F87171",
    }
    p["link"] = _tint(p["accent"], 0.45)
    p["link_hover"] = _tint(p["accent"], 0.65)
    p["accent_hover"] = _tint(p["accent"], 0.15)
    family = p["family"]
    icon = _icon_font()

    winchrome.enable_dpi_awareness()
    dialog = tk.Toplevel(parent)
    dialog.withdraw()
    dialog.overrideredirect(True)          # 去掉系统标题栏（它会是一条亮白条）
    dialog.configure(bg=bg)
    dialog.attributes("-topmost", True)
    dialog.title(title)                    # 供窗口查找 / 辅助工具识别

    # ─────────────────────── 自绘标题栏（可拖动）───────────────────────
    header = tk.Frame(dialog, bg=bg)
    header.pack(fill="x")
    text_col = tk.Frame(header, bg=bg)
    text_col.pack(side="left", fill="x", expand=True, padx=20, pady=(13, 10))
    title_label = tk.Label(
        text_col, text=title, bg=bg, fg=p["fg"], font=(family, 11, "bold"), anchor="w",
    )
    title_label.pack(fill="x")
    header_widgets = [header, text_col, title_label]
    if subtitle:
        sub_label = tk.Label(
            text_col, text=subtitle, bg=bg, fg=p["faint"], font=(family, 8),
            anchor="w", justify="left", wraplength=430,
        )
        sub_label.pack(fill="x", pady=(5, 0))
        header_widgets.append(sub_label)
    close_btn = tk.Label(
        header, text=GLYPH_CLOSE, bg=bg, fg=p["dim"], font=(icon, 11),
        padx=18, cursor="hand2",
    )
    close_btn.pack(side="right", anchor="n", pady=14)

    drag_origin: dict = {}

    def drag_start(event) -> None:
        drag_origin["x"], drag_origin["y"] = event.x_root, event.y_root
        drag_origin["wx"], drag_origin["wy"] = dialog.winfo_x(), dialog.winfo_y()

    def drag_move(event) -> None:
        if "x" not in drag_origin:
            return
        x = drag_origin["wx"] + (event.x_root - drag_origin["x"])
        y = drag_origin["wy"] + (event.y_root - drag_origin["y"])
        dialog.geometry(f"+{x}+{y}")

    for widget in header_widgets:
        widget.bind("<ButtonPress-1>", drag_start)
        widget.bind("<B1-Motion>", drag_move)

    # ──────────────────────────── 控件工厂 ────────────────────────────
    def card(parent_frame: tk.Misc, title_text: str, desc_text: str = "") -> tk.Frame:
        """一块卡片：比背景略亮的圆角容器（圆角靠窗口裁，卡片本身是矩形）。"""
        block = tk.Frame(parent_frame, bg=p["bg"])
        block.pack(fill="x", pady=(0, 11))
        box = tk.Frame(block, bg=p["card"])
        box.pack(fill="x")
        inner = tk.Frame(box, bg=p["card"])
        inner.pack(fill="x", padx=15, pady=12)
        # 把外层容器挂在 inner 上：首次配置模式要按「整块卡片」筛掉不需要的，
        # 而卡片容器在页面里的层级比 inner 高
        inner.block = block
        tk.Label(
            inner, text=title_text, bg=p["card"], fg=p["fg"],
            font=(family, 10, "bold"), anchor="w",
        ).pack(fill="x")
        if desc_text:
            tk.Label(
                inner, text=desc_text, bg=p["card"], fg=p["faint"],
                font=(family, 8), anchor="w", justify="left", wraplength=470,
            ).pack(fill="x", pady=(3, 0))
        return inner

    def head(inner: tk.Frame, title_text: str, desc_text: str = "") -> tk.Frame:
        """卡片里的一行：左边标题（+说明），右边放控件。"""
        line = tk.Frame(inner, bg=p["card"])
        line.pack(fill="x", pady=(10, 0))
        left = tk.Frame(line, bg=p["card"])
        left.pack(side="left", fill="x", expand=True, anchor="n")
        tk.Label(
            left, text=title_text, bg=p["card"], fg=p["fg"],
            font=(family, 10), anchor="w", justify="left",
        ).pack(fill="x")
        if desc_text:
            tk.Label(
                left, text=desc_text, bg=p["card"], fg=p["faint"],
                font=(family, 8), anchor="w", justify="left", wraplength=400,
            ).pack(fill="x", pady=(2, 0))
        holder = tk.Frame(line, bg=p["card"])
        holder.pack(side="right", anchor="n", padx=(12, 0))
        holder.row_frame = line
        return holder

    def full(inner: tk.Frame, title_text: str, desc_text: str = "") -> tk.Frame:
        """滑块用：标题与值一行，轨道独占下一行（塞在右侧会挤成一小段）。"""
        line = tk.Frame(inner, bg=p["card"])
        line.pack(fill="x", pady=(10, 0))
        top = tk.Frame(line, bg=p["card"])
        top.pack(fill="x")
        tk.Label(
            top, text=title_text, bg=p["card"], fg=p["fg"], font=(family, 10), anchor="w"
        ).pack(side="left")
        value_label = tk.Label(
            top, text="", bg=p["card"], fg=p["dim"], font=(family, 10), anchor="e"
        )
        value_label.pack(side="right")
        if desc_text:
            tk.Label(
                line, text=desc_text, bg=p["card"], fg=p["faint"],
                font=(family, 8), anchor="w", justify="left", wraplength=470,
            ).pack(fill="x", pady=(2, 0))
        track = tk.Canvas(line, height=22, bg=p["card"], highlightthickness=0, bd=0)
        track.pack(fill="x", pady=(6, 0))
        track.value_label = value_label
        track.line_frame = line
        return track

    def slider(track: tk.Canvas, var: tk.DoubleVar, low: float, high: float, suffix: str = ""):
        state = {"dimmed": False}

        def draw() -> None:
            track.delete("all")
            width = max(track.winfo_width(), 80)
            middle = 11
            span = (high - low) or 1.0
            ratio = min(1.0, max(0.0, (var.get() - low) / span))
            knob = 11 + ratio * (width - 22)
            dimmed = state["dimmed"]
            track.create_line(
                11, middle, width - 11, middle,
                fill=p["line"] if not dimmed else "#2E2E2E", width=5, capstyle="round",
            )
            if knob > 11.5:
                track.create_line(
                    11, middle, knob, middle,
                    fill=p["accent"] if not dimmed else "#3A3A3A", width=5, capstyle="round",
                )
            track.create_oval(
                knob - 8, middle - 8, knob + 8, middle + 8,
                fill=p["fg"] if not dimmed else p["faint"],
                outline=p["card"], width=3,
            )
            number = var.get()
            text = (
                f"{number:.2f}" if abs(number - round(number)) > 1e-9 else f"{int(round(number))}"
            )
            track.value_label.configure(
                text=f"{text}{suffix}", fg=p["dim"] if not dimmed else p["faint"]
            )

        def set_from_x(x: int) -> None:
            if state["dimmed"]:
                return
            width = max(track.winfo_width(), 80)
            ratio = min(1.0, max(0.0, (x - 11) / max(1, width - 22)))
            value = low + ratio * (high - low)
            if high - low > 20:
                value = round(value)
            if high - low <= 1.5:
                value = round(value, 2)
            var.set(value)
            draw()

        track.bind("<Button-1>", lambda event: set_from_x(event.x))
        track.bind("<B1-Motion>", lambda event: set_from_x(event.x))
        track.bind("<Configure>", lambda _event: draw())
        draw()

        def set_dimmed(flag: bool) -> None:
            state["dimmed"] = bool(flag)
            draw()

        return {"set_dimmed": set_dimmed, "redraw": draw}

    def toggle(holder: tk.Frame, var: tk.BooleanVar, command=None) -> None:
        """拨动开关（pill switch）。比方框复选框现代得多。"""
        width, height = 44, 24
        canvas = tk.Canvas(
            holder, width=width, height=height, bg=p["card"],
            highlightthickness=0, bd=0, cursor="hand2",
        )
        canvas.pack(side="right")

        def draw() -> None:
            canvas.delete("all")
            on = var.get()
            canvas.create_line(
                height // 2, height // 2, width - height // 2, height // 2,
                fill=p["accent"] if on else p["line"], width=height - 4, capstyle="round",
            )
            x = width - height // 2 if on else height // 2
            canvas.create_oval(
                x - height // 2 + 3, 3, x + height // 2 - 3, height - 3,
                fill="#FFFFFF" if on else p["dim"], outline="",
            )

        def flip(_event=None) -> None:
            var.set(not var.get())
            draw()
            if command is not None:
                command(var.get())

        canvas.bind("<Button-1>", flip)
        draw()

    def dropdown(holder: tk.Frame, var: tk.StringVar, values: list, labels: dict | None = None) -> None:
        box = tk.Frame(holder, bg=p["line"])
        box.pack(side="right")
        inner = tk.Frame(box, bg=p["field"])
        inner.pack(padx=1, pady=1)
        menu = tk.Menu(
            inner, tearoff=0, bg=p["field"], fg=p["fg"], activebackground=p["accent"],
            activeforeground="#FFFFFF", font=(family, 10), bd=0, activeborderwidth=0,
        )
        shown = tk.StringVar(value=(labels or {}).get(var.get(), var.get()))

        def choose(value: str) -> None:
            var.set(value)
            shown.set((labels or {}).get(value, value))

        for value in values:
            menu.add_radiobutton(
                label=(labels or {}).get(value, value),
                command=lambda v=value: choose(v), font=(family, 10),
            )
        tk.Menubutton(
            inner, textvariable=shown, menu=menu, bg=p["field"], fg=p["fg"],
            activebackground=p["field"], activeforeground=p["fg"], relief="flat", bd=0,
            highlightthickness=0, anchor="w", font=(family, 10), cursor="hand2",
            padx=10, pady=5, width=17,
        ).pack(side="left")
        tk.Label(inner, text="▾", bg=p["field"], fg=p["faint"], font=(family, 11)).pack(
            side="left", padx=(2, 9)
        )

    def entry(holder: tk.Frame, var: tk.StringVar, secret: bool = False, width: int = 26) -> tk.Entry:
        box = tk.Frame(holder, bg=p["line"])
        box.pack(side="right")
        widget = tk.Entry(
            box, textvariable=var, show="•" if secret else "", font=("Consolas", 10),
            bg=p["field"], fg=p["fg"], insertbackground=p["fg"], relief="flat", bd=0,
            highlightthickness=0, width=width,
        )
        widget.pack(padx=1, pady=1, ipady=5, ipadx=8)
        return widget

    # ───────────────────────────── 变量 ─────────────────────────────
    key_var = tk.StringVar(value=cfg.api_key)
    model_var = tk.StringVar(value=str(cfg.get("deepseek.model", MODELS[0])))
    show_key_var = tk.BooleanVar(value=False)
    workers_var = tk.DoubleVar(value=int(cfg.get("translate.workers", 4)))
    context_var = tk.DoubleVar(value=int(cfg.get("translate.context_sentences", 3)))
    speculate_var = tk.BooleanVar(value=bool(cfg.get("translate.speculate", True)))
    speculate_ms_var = tk.DoubleVar(value=int(cfg.get("translate.speculate_ms", 500)))
    font_var = tk.DoubleVar(value=int(cfg.get("overlay.font_size", 21)))
    source_font_var = tk.DoubleVar(value=int(cfg.get("overlay.source_font_size", 13)))
    padding_var = tk.DoubleVar(value=int(cfg.get("overlay.padding", 26)))
    rows_var = tk.DoubleVar(value=int(cfg.get("overlay.max_sentences", 2)))
    alpha_var = tk.DoubleVar(value=float(cfg.get("overlay.alpha", 0.9)))
    position_var = tk.StringVar(value=str(cfg.get("overlay.position", "bottom-center")))
    show_source_var = tk.BooleanVar(value=bool(cfg.get("overlay.show_source", True)))
    click_var = tk.BooleanVar(value=bool(cfg.get("overlay.click_through", False)))
    close_lc_var = tk.BooleanVar(value=bool(cfg.get("overlay.close_livecaptions", True)))
    stable_var = tk.DoubleVar(value=int(cfg.get("captions.stable_ms", 1400)))
    clause_var = tk.DoubleVar(value=int(cfg.get("captions.clause_min_words", 12)))
    cache_var = tk.BooleanVar(value=bool(cfg.get("cache.enabled", True)))
    phrasebook_var = tk.BooleanVar(value=bool(cfg.get("phrasebook.enabled", True)))
    cache_size_var = tk.DoubleVar(value=int(cfg.get("cache.size", 500)))
    auto_launch_var = tk.BooleanVar(value=bool(cfg.get("captions.auto_launch", True)))
    log_raw_var = tk.BooleanVar(value=bool(cfg.get("captions.log_raw_text", True)))
    lang_var = tk.StringVar(value=str(cfg.get("translate.target_language", "简体中文")))
    temp_var = tk.DoubleVar(value=float(cfg.get("deepseek.temperature", 1.3)))
    timeout_var = tk.DoubleVar(value=int(cfg.get("deepseek.timeout_seconds", 25)))

    body = tk.Frame(dialog, bg=bg)
    body.pack(fill="x")
    tabs = _Tabs(body, p)

    # ───────────────────────── 第 1 页：翻译 ─────────────────────────
    page_conn = tabs.add("conn", "翻译")
    deepseek = card(page_conn, "DeepSeek", "翻译由 DeepSeek 完成，Key 只保存在本机 config.json。")
    key_row = head(deepseek, "API Key")
    key_entry = entry(key_row, key_var, secret=True)
    link_line = tk.Frame(deepseek, bg=p["card"])
    link_line.pack(fill="x", pady=(9, 0))

    def on_show_toggle(_value: bool) -> None:
        key_entry.configure(show="" if show_toggle.get() else "•")

    show_toggle = tk.BooleanVar(value=False)
    toggle(link_line, show_toggle, command=on_show_toggle)
    tk.Label(
        link_line, text="显示 Key", bg=p["card"], fg=p["faint"], font=(family, 9)
    ).pack(side="right", padx=(0, 10))
    link = tk.Label(
        link_line, text="获取 API Key ↗", bg=p["card"], fg=p["link"],
        font=(family, 9), cursor="hand2",
    )
    link.pack(side="left")
    link.bind("<Button-1>", lambda _event: webbrowser.open(API_KEYS_URL))
    link.bind("<Enter>", lambda _event: link.configure(fg=p["link_hover"]))
    link.bind("<Leave>", lambda _event: link.configure(fg=p["link"]))

    model_holder = head(deepseek, "模型")
    dropdown(model_holder, model_var, MODELS)

    speed = card(page_conn, "速度", "跟不上就调大并发；推测翻译能把断句等待藏起来。")
    slider(full(speed, "并发翻译线程", "同时向 DeepSeek 发几条请求"), workers_var, 1, 8, " 路")
    spec_holder = head(speed, "推测翻译", "在句子定稿前先翻译，命中即零等待")
    toggle(spec_holder, speculate_var)
    threshold = full(speed, "推测触发延迟", "越小越早翻译，白烧的请求也越多")
    handle = slider(threshold, speculate_ms_var, 200, 1200, " ms")
    slider(full(speed, "携带上文句数", "让术语与代词保持一致"), context_var, 0, 8, " 句")

    tune = card(page_conn, "模型参数", "一般不用动。改这些需要重启。")
    entry(head(tune, "目标语言", "译文使用的语言"), lang_var, width=14)

    # ───────────────────────── 第 2 页：外观 ─────────────────────────
    page_look = tabs.add("look", "外观")
    look = card(page_look, "字幕", "这些改动保存后立即生效，不用重启。")
    slider(full(look, "译文字号"), font_var, 12, 36, " 磅")
    slider(full(look, "原文字号"), source_font_var, 8, 28, " 磅")
    slider(full(look, "文字边距", "文字到边框的距离"), padding_var, 4, 60, " px")
    slider(full(look, "同时显示", "每句含原文 + 译文两行"), rows_var, 1, 4, " 句")
    toggle(head(look, "英文原文", "在译文上方显示英文"), show_source_var)

    window = card(page_look, "窗口")
    slider(full(window, "不透明度", "1.0 为完全不透明"), alpha_var, 0.3, 1.0)
    dropdown(head(window, "初始位置", "拖动浮窗后以拖动的位置为准"), position_var, POSITIONS, POSITION_LABELS)
    toggle(head(window, "鼠标穿透", "点击直接落到视频上（此时按钮点不到）"), click_var)
    toggle(head(window, "退出时关字幕", "关闭翻译时一并关掉系统实时字幕"), close_lc_var)

    # ─────────────────── 第 3 页：字幕与缓存 ───────────────────
    page_adv = tabs.add("adv", "字幕与缓存")
    cut = card(page_adv, "断句", "决定一句话什么时候算说完。改这些需要重启。")
    slider(full(cut, "稳定阈值", "越小越快，太小会把句子切碎"), stable_var, 600, 2400, " ms")
    slider(full(cut, "逗号从句门槛", "长句在逗号处提前定稿；0 表示关闭"), clause_var, 0, 24, " 词")

    fast = card(page_adv, "提速", "重复出现的短句不必再请求一次 API。")
    toggle(head(fast, "翻译缓存", "AI 学到的短句译文，直接复用"), cache_var, command=on_cache_toggle)
    toggle(head(fast, "内置短语包", "高频套话零延迟命中"), phrasebook_var)
    slider(full(fast, "缓存容量", "最多记住多少条短句"), cache_size_var, 100, 2000, " 条")
    stats_var = tk.StringVar(value="统计：累计中…")
    tk.Label(
        fast, textvariable=stats_var, bg=p["card"], fg=p["faint"],
        font=(family, 9), anchor="w",
    ).pack(fill="x", pady=(12, 0))

    runtime = card(page_adv, "运行方式", "启动与记录行为。")
    toggle(head(runtime, "自动启动实时字幕", "没在运行时自动帮你打开"), auto_launch_var)
    toggle(head(runtime, "记录原始字幕", "写入 logs/ 便于事后排查（analyze_log.py 用）"), log_raw_var)

    tabs.finish()
    dialog.tabs = tabs

    # ────────────────────────── 底部操作栏 ──────────────────────────
    tk.Frame(dialog, bg=p["line"], height=1).pack(fill="x", padx=24, pady=(8, 0))
    footer = tk.Frame(dialog, bg=bg)
    footer.pack(fill="x", padx=24, pady=(12, 16))
    status = tk.Label(
        footer, text=f"当前：{_mask(cfg.api_key)}", bg=bg, fg=p["dim"],
        font=(family, 9), anchor="w", justify="left", wraplength=320,
    )
    status.pack(side="left", fill="x", expand=True)

    result: dict = {}

    def close() -> None:
        # 先取消统计定时器，否则它会在窗口销毁后触发并抛 TclError
        job = stats_job.get("id")
        if job is not None:
            try:
                dialog.after_cancel(job)
            except Exception:
                pass
            stats_job["id"] = None
        dialog.destroy()

    close_btn.bind("<Button-1>", lambda _event: close())
    close_btn.bind("<Enter>", lambda _event: close_btn.configure(fg="#F87171"))
    close_btn.bind("<Leave>", lambda _event: close_btn.configure(fg=p["dim"]))

    def gather() -> dict:
        return {
            "deepseek.model": model_var.get().strip(),
            "translate.workers": int(workers_var.get()),
            "translate.context_sentences": int(context_var.get()),
            "translate.speculate": bool(speculate_var.get()),
            "translate.speculate_ms": int(speculate_ms_var.get()),
            "overlay.font_size": int(font_var.get()),
            "overlay.source_font_size": int(source_font_var.get()),
            "overlay.padding": int(padding_var.get()),
            "overlay.max_sentences": int(rows_var.get()),
            "overlay.alpha": round(float(alpha_var.get()), 2),
            "overlay.position": position_var.get(),
            "overlay.show_source": bool(show_source_var.get()),
            "overlay.click_through": bool(click_var.get()),
            "overlay.close_livecaptions": bool(close_lc_var.get()),
            "captions.stable_ms": int(stable_var.get()),
            "captions.clause_min_words": int(clause_var.get()),
            "cache.enabled": bool(cache_var.get()),
            "phrasebook.enabled": bool(phrasebook_var.get()),
            "cache.size": int(cache_size_var.get()),
            "captions.auto_launch": bool(auto_launch_var.get()),
            "captions.log_raw_text": bool(log_raw_var.get()),
            "translate.target_language": lang_var.get().strip() or "简体中文",
        }

    def apply_and_close() -> None:
        values = gather()
        api_key = key_var.get().strip()
        values["deepseek.api_key"] = api_key
        try:
            if cfg.path is not None:
                save_values(cfg.path, values)
        except OSError as exc:
            status.configure(text=f"写入配置文件失败：{exc}", fg=p["bad"])
            save_button.configure(state="normal", text="保存")
            return
        for dotted, value in values.items():
            cfg.set(dotted, value)
        if on_saved is not None:
            on_saved(api_key, values["deepseek.model"])
        if on_apply is not None:
            try:
                on_apply()
            except Exception:
                pass
        status.configure(text="已保存，外观类改动立即生效。", fg=p["good"])
        dialog.after(420, close)

    def poll() -> None:
        if "done" not in result:
            dialog.after(120, poll)
            return
        ok, message = result.pop("done")
        if not ok:
            status.configure(text=message, fg=p["bad"])
            save_button.configure(state="normal", text="保存")
            return
        apply_and_close()

    def save() -> None:
        api_key = key_var.get().strip()
        if not api_key:
            status.configure(text="请先填写 API Key", fg=p["bad"])
            key_entry.focus_set()
            return
        if api_key == (cfg.api_key or "").strip():
            # Key 没改就别再联网校验一遍，纯属浪费
            apply_and_close()
            return
        save_button.configure(state="disabled", text="校验中…")
        status.configure(text="正在校验新的 API Key …", fg=p["dim"])
        result.clear()
        base = str(cfg.get("deepseek.base_url", "https://api.deepseek.com"))
        model = model_var.get().strip()

        def work() -> None:
            result["done"] = probe_api_key(base, api_key, model)

        threading.Thread(target=work, daemon=True).start()
        dialog.after(120, poll)

    save_button = tk.Button(
        footer, text="保存", command=save, bg=p["accent"], fg="#FFFFFF",
        activebackground=p["accent_hover"], activeforeground="#FFFFFF",
        relief="flat", bd=0, font=(family, 10), padx=22, pady=6, cursor="hand2",
    )
    save_button.pack(side="right")
    tk.Button(
        footer, text="取消", command=close, bg="#3A3A3A", fg=p["fg"],
        activebackground="#4A4A4A", activeforeground=p["fg"], relief="flat", bd=0,
        font=(family, 10), padx=22, pady=6, cursor="hand2",
    ).pack(side="right", padx=(0, 10))

    # 首次配置：只留 DeepSeek 卡片里的 Key 与模型，其余一律去掉
    if simple:
        keep = {key_row.row_frame, link_line, model_holder.row_frame}
        for block in list(page_conn.winfo_children()):
            if block is not deepseek.block:
                block.destroy()
        tabs.bar.pack_forget()
        for _label, _underline, content in tabs._items.values():
            content.pack_forget()
        page_conn.pack(fill="both", expand=True)
        for widget in list(deepseek.winfo_children()):
            if widget in keep or isinstance(widget, tk.Label):
                continue
            widget.destroy()

    # 统计刷新。必须能被关闭时取消：定时器在对话框销毁后触发会抛
    # TclError: invalid command name "...refresh_stats"（用户关窗时会看到）。
    stats_job: dict = {}

    def refresh_stats() -> None:
        try:
            if not dialog.winfo_exists():
                return
            if cache_stats_provider is not None:
                stats = cache_stats_provider()
                hits, misses = stats[0], stats[1]
                preset = stats[2] if len(stats) > 2 else 0
                stats_var.set(f"短语包命中 {preset}　缓存命中 {hits}　未命中 {misses}")
        except Exception:
            pass
        finally:
            if dialog.winfo_exists():
                try:
                    stats_job["id"] = dialog.after(900, refresh_stats)
                except Exception:
                    pass

    stats_job["id"] = dialog.after(900, refresh_stats)

    def cancel_stats(_event=None) -> None:
        job = stats_job.get("id")
        if job is not None:
            try:
                dialog.after_cancel(job)
            except Exception:
                pass
            stats_job["id"] = None

    # 绑到 <Destroy> 上：定时器注册的是 Tcl 层命令，widget 销毁后再触发，
    # 错误在 Tcl 层就抛出来了，Python 侧的 winfo_exists() 守卫根本来不及执行。
    # 只有销毁前取消才可靠，而 <Destroy> 能覆盖所有销毁路径（按钮、Esc、程序化）。
    def on_destroy(event) -> None:
        if event.widget is dialog:
            cancel_stats()

    dialog.bind("<Destroy>", on_destroy, add="+")

    # ───────────────── 圆角 + 定位（去掉系统标题栏后自己做）─────────────────
    # 注意：这里**不能**调 update_idletasks()。改窗口区域会再次触发 <Configure>，
    # 而在 <Configure> 处理里刷新布局就会无限递归、把整个界面卡死。
    chrome_busy = {"flag": False}

    def apply_chrome() -> None:
        if chrome_busy["flag"]:
            return
        chrome_busy["flag"] = True
        try:
            width, height = dialog.winfo_width(), dialog.winfo_height()
            if width <= 1 or height <= 1:
                return
            # 窗口是 overrideredirect 的，winfo_id() 本身就是顶层句柄
            winchrome.round_window_corners(int(dialog.winfo_id()), width, height, 12)
            winchrome.keep_topmost(int(dialog.winfo_id()))
        finally:
            chrome_busy["flag"] = False

    def center() -> None:
        dialog.update_idletasks()
        width, height = dialog.winfo_width(), dialog.winfo_height()
        parent_ok = False
        try:
            parent_ok = bool(parent.winfo_viewable())
        except Exception:
            parent_ok = False
        if parent_ok:
            x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        else:
            x = (dialog.winfo_screenwidth() - width) // 2
            y = (dialog.winfo_screenheight() - height) // 3
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")

    center()
    dialog.deiconify()

    def fit_height() -> None:
        """按当前页的内容重新定高（窗口宽度不变，只有底边会动）。"""
        dialog.update_idletasks()
        dialog.geometry(f"{dialog.winfo_width()}x{dialog.winfo_reqheight()}")
        apply_chrome()

    tabs.on_resize = fit_height
    tabs.select("conn")
    dialog.update_idletasks()
    apply_chrome()
    dialog.bind("<Configure>", lambda _event: apply_chrome())

    key_entry.focus_set()
    dialog.bind("<Escape>", lambda _event: close())
    return dialog


def open_settings_dialog(
    parent: tk.Misc,
    cfg: Config,
    on_saved=None,
    on_cache_toggle=None,
    cache_stats_provider=None,
    on_apply=None,
    title: str = "设置",
    subtitle: str | None = None,
    simple: bool = False,
) -> tk.Toplevel:
    """打开设置面板。

    ``simple=True`` 用于首次配置：只留 API Key 与模型，不分页、不显示其它设置。
    ``on_saved(api_key, model)`` 与 ``on_apply()`` 在保存成功后回调。
    """
    return _build_dialog(
        parent,
        cfg,
        on_saved=on_saved,
        on_cache_toggle=on_cache_toggle,
        cache_stats_provider=cache_stats_provider,
        on_apply=on_apply,
        title=title,
        subtitle=subtitle,
        simple=simple,
    )


def prompt_for_api_key(cfg: Config, first_run: bool = True) -> str | None:
    """用**图形窗口**让用户填写 API Key（不依赖终端）。

    返回填好的 Key；用户在窗口里取消 / 直接关窗时返回空串。
    """
    saved: dict = {}
    root = tk.Tk()
    root.withdraw()
    try:
        dialog = _build_dialog(
            root,
            cfg,
            on_saved=lambda api_key, _model: saved.update(key=api_key),
            title="首次配置" if first_run else "设置",
            subtitle=(
                "还没有配置 DeepSeek API Key，填好就能开始翻译。\n"
                "Key 只保存在本机的 config.json 里，不会上传到别处。"
                if first_run
                else None
            ),
            simple=first_run,
        )
        root.wait_window(dialog)
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return saved.get("key", "")


def show_message(title: str, message: str) -> None:
    """用一个图形小窗口提示信息（终端里跑的用户看不到退出后就被关掉的打印）。"""
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        messagebox.showinfo(title, message, parent=root)
    finally:
        try:
            root.destroy()
        except Exception:
            pass

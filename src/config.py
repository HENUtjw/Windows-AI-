#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""配置加载。

配置文件为项目根目录下的 config.json，缺省项由 DEFAULT_CONFIG 补齐，
因此用户只需写自己关心的字段。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"

DEFAULT_SYSTEM_PROMPT = (
    "你是一个专业的视频字幕翻译引擎，负责把英文字幕实时翻译成{target}。\n"
    "必须遵守以下规则：\n"
    "1. 只输出译文本身。不要输出原文、音标、解释、注释或任何前后缀。\n"
    "2. 译文要自然、口语化，符合中文母语者的表达习惯，适合直接作为视频字幕显示。\n"
    "3. 输入来自语音识别，可能缺少标点、存在识别错误或句子不完整。"
    "请结合上下文合理推断语义，不要逐词硬译，也不要添加原文没有的信息。\n"
    "4. 译文长度要与原文相当，不要扩写、不要补充解释——字幕空间有限。\n"
    "5. 人名、地名、专有名词、数字和单位必须准确。\n"
    "6. 如果输入只是语气词、填充词或无法理解的片段，输出最贴近的中文口语即可，不要留空。"
)

DEFAULT_CONFIG: dict[str, Any] = {
    "deepseek": {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        # 官方现行模型名是 deepseek-flash / deepseek-v4-pro，
        # 旧的 deepseek-chat / deepseek-reasoner 已停用（会返回 400）。
        "model": "deepseek-flash",
        # 新版模型**默认开启思考模式**。字幕翻译必须关掉，否则每句都要先推理
        # 一遍，延迟和费用都会高出一个数量级。留空表示不发送该参数。
        "thinking": "disabled",
        "temperature": 1.3,
        # 一句字幕不可能很长；限一个上限可以避免偶发的长输出拖慢整句
        "max_tokens": 256,
        "timeout_seconds": 25,
        "max_retries": 2,
        "stream": True,
    },
    "translate": {
        "target_language": "简体中文",
        "context_sentences": 3,
        # 并发翻译线程数。实测 DeepSeek 单句响应在 0.9s–4.6s 之间波动，
        # 单线程串行时一旦 API 变慢、视频又出句快，队列就会越积越长（字幕越拖越远）。
        # 实测：每 1.5s 出句时 3 路足够；每 1.0s 出句（很快的对话）时 3 路开始吃力、
        # 5 路平稳。默认取 4 留出余量。加大它不影响费用（请求数不变），
        # 只是「上文」可能略微滞后，而上文只用于提升一致性。
        "workers": 4,
        # 翻译跟不上说话速度时的保护：待翻译队列超过这个长度就丢掉最旧的，
        # 保证显示的字幕始终贴近「当前时刻」，而不是越拖越远。
        "max_backlog": 3,
        # 推测翻译：句子正式定稿之前，先把「词已经稳定」的半句送去翻译。
        # 定稿时若命中原推测（实测命中率 86%），译文已经就绪 —— 等于把断句等待
        # （约 1.6s）藏了起来。代价是请求数约增加 2 倍，按 flash 的价格可忽略。
        "speculate": True,
        "speculate_ms": 500,
        "system_prompt": "",
    },
    "captions": {
        "poll_interval_ms": 120,
        # 实测（真实录制数据回放）该值是「最快且不切碎句子」的平衡点：
        #   1400ms → 平均额外等待 1.60s，最坏 1.96s，输出 7 句全干净
        #   1200ms 及以下 → 出现 "That trade." 这类碎片
        #   1600/1800ms → 平均 2.7s、最坏 7.25s（掉进实时字幕补标点的边界）
        "stable_ms": 1400,
        # 长句提速：逗号也可作为从句边界，但要求前面已有这么多词。
        # 长句若只认句末标点，要等整句说完才出现（实测可超过 8 秒），而且推测
        # 翻译永远触发不了。12 是「能切出有意义的从句」与「不切碎列表/However,」
        # 之间的平衡点。设为 0 关闭。
        "clause_min_words": 12,
        "auto_launch": True,
        "log_raw_text": True,
        "log_dir": "logs",
        "reconnect_interval_seconds": 2.0,
    },
    "overlay": {
        "enabled": True,
        "font_family": "Microsoft YaHei UI",
        "font_size": 21,
        "source_font_size": 13,
        "show_source": True,
        "alpha": 0.9,
        # 仿 Windows 实时字幕的深色玻璃面板
        "background": "#1C1C1C",
        "border_color": "#3A3A3A",
        "corner_radius": 12,
        "text_color": "#FFFFFF",
        "source_color": "#9AA0A6",
        "icon_color": "#C8C8C8",
        "icon_hover_color": "#FFFFFF",
        "width": 1400,
        "position": "bottom-center",
        "margin_bottom": 90,
        "margin_top": 60,
        "margin_x": 40,
        # 注意：鼠标穿透与「按钮可点 / 面板可拖」互斥，所以默认关闭。
        # 想在看视频时彻底不误触，用热键 Ctrl+Alt+T 临时打开。
        "click_through": False,
        "show_buttons": True,
        # 关闭浮窗时一并关掉 Windows 实时字幕
        "close_livecaptions": True,
        "max_sentences": 2,
        # 文字到边框的距离（四边统一）。调大更透气，调小更紧凑。
        "padding": 26,
        "line_spacing": 6,
        # 句与句之间留更大的间距，否则多行均匀排布看不出哪句配哪句
        "group_spacing": 16,
        # 译文还没到时的占位文字颜色
        "placeholder_color": "#6B7280",
        # 设置面板的配色。全部从这里取，避免出现「两种互不相干的蓝/灰」。
        "accent_color": "#2D6CDF",
        "field_background": "#262626",
        "field_border": "#4A4A4A",
    },
    "cache": {
        # 短语级翻译缓存（AI 学来的）：同一短句的译文获得足够票数后晋升，
        # 之后相同短句（≤ max_words 词）直接复用译文、跳过 API 调用。
        "enabled":   True,
        "max_words": 6,
        "min_hits":  3,
        "size":      500,
        # 晋升还要求最高票译文占比达到这个比例。
        # 实测 temperature=1.3 下同一短句会有多种译法：完全稳定的
        # （Yeah. / Okay.）占比接近 100%，而语境依赖的（Right. / Of course.）
        # 最高票只有 50%。0.75 正好把后者挡在缓存外。
        "majority":  0.75,
        # 至少观察这么多次才开始评估晋升。没有它会在「早期走运」时误判：
        # 实测 Right. 在第 4 次观察时恰好凑成 3/4 = 75%，会带着 50% 的
        # 真实分布被晋升。
        "min_samples": 5,
    },
    "phrasebook": {
        # 内置高频短语包：翻译链路的第一层，命中即用（零延迟、零成本）。
        # 只收录脱离上下文也唯一正确的短语，所以不需要运行时再用 AI 复核。
        "enabled": True,
        # 留空使用 data/phrasebook.json；可指向自己的文件扩充。
        "path":    "",
    },
}


def _update_config_file(path: Path | str, mutate) -> None:
    """读取 config.json、交给 ``mutate`` 修改、再写回（保留其他配置项）。

    读取用 ``utf-8-sig`` 兼容带 BOM 的文件；写回不带 BOM。
    ``path`` 允许传字符串——公开 API 不该因为调用方少包一层 ``Path()`` 就崩。
    """
    path = Path(path)
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    mutate(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_values(path: Path | str, updates: dict) -> None:
    """按**点号路径**批量写回配置，例如::

        save_values(path, {"overlay.font_size": 24, "cache.enabled": False})

    设置面板用它一次性保存所有改动，不必为每一项单独写函数。
    中间层不存在会自动创建，同级其它配置项保持不变。
    """

    def mutate(data: dict) -> None:
        for dotted, value in updates.items():
            section, _, leaf = str(dotted).rpartition(".")
            if not leaf:
                continue
            target = data
            for part in section.split("."):
                if not part:
                    continue
                child = target.get(part)
                if not isinstance(child, dict):
                    child = {}
                    target[part] = child
                target = child
            target[leaf] = value

    _update_config_file(path, mutate)


def save_api_key(path: Path | str, api_key: str, model: str | None = None) -> None:
    """把 API Key（可选模型名）写入配置文件。"""

    def mutate(data: dict) -> None:
        section = data.get("deepseek")
        if not isinstance(section, dict):
            section = {}
            data["deepseek"] = section
        section["api_key"] = api_key
        if model:
            section["model"] = model

    _update_config_file(path, mutate)


def save_overlay_position(path: Path, x: int, y: int) -> None:
    """记住用户拖动后的浮窗位置。"""

    def mutate(data: dict) -> None:
        section = data.get("overlay")
        if not isinstance(section, dict):
            section = {}
            data["overlay"] = section
        section["position"] = "custom"
        section["x"] = int(x)
        section["y"] = int(y)

    _update_config_file(path, mutate)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并，override 覆盖 base，返回新字典。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config:
    """带点号路径访问的配置对象。"""

    def __init__(self, data: dict[str, Any] | None = None, path: Path | None = None) -> None:
        # 始终与默认值合并，保证任何 Config 实例都能读到全部字段
        self.data = _deep_merge(DEFAULT_CONFIG, data or {})
        self.path = path

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        config_path = Path(path).expanduser().resolve() if path else DEFAULT_CONFIG_PATH
        user_data: dict[str, Any] = {}
        if config_path.exists():
            try:
                # 用 utf-8-sig 读取：PowerShell 的 `Set-Content -Encoding UTF8`
                # 以及部分编辑器保存的 UTF-8 文件都会带 BOM，用 utf-8 直接解析
                # 会报 "Unexpected UTF-8 BOM"，而提示完全看不出真正原因。
                user_data = json.loads(config_path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"配置文件格式错误：{config_path}\n  {exc}\n"
                    "提示：JSON 不允许注释和结尾多余的逗号；"
                    "字符串必须用双引号，路径中的反斜杠要写成 \\\\。"
                ) from exc
        return cls(user_data, config_path)

    # ------------------------------------------------------------------ #
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    # ------------------------------------------------------------------ #
    @property
    def api_key(self) -> str:
        """配置文件优先，其次环境变量 DEEPSEEK_API_KEY。"""
        return (self.get("deepseek.api_key") or os.environ.get("DEEPSEEK_API_KEY", "")).strip()

    @property
    def system_prompt(self) -> str:
        custom = (self.get("translate.system_prompt") or "").strip()
        if custom:
            return custom
        return DEFAULT_SYSTEM_PROMPT.format(target=self.get("translate.target_language", "简体中文"))

    def resolve_path(self, value: str) -> Path:
        """把配置里的相对路径解析为项目根目录下的绝对路径。"""
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate

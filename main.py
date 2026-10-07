#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 视频翻译 —— 程序入口。

把 Windows 实时字幕识别出的英文，用 DeepSeek 实时翻译成中文并显示在置顶浮窗里。

常用命令::

    python main.py                # 正常使用（没配 Key 会直接引导填写）
    python main.py --setup        # 交互式填写 / 更换 API Key
    python main.py --check-api    # 验证 Key、模型名与网络
    python main.py --demo         # 演示模式：回放脚本字幕 + 模拟翻译，不需要 API Key
    python main.py --console      # 不显示浮窗，只在控制台输出
    python main.py -v             # 输出调试日志
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PROJECT_ROOT)

# 控制台输出兜底。
# 中文 Windows 控制台默认 cp936，交互式时 Python 走 WriteConsoleW 不会有问题；
# 但一旦输出被重定向（管道 / 文件），就会按 cp936 编码，遇到 ⚙ ✕ ⚠ 这类符号
# 直接抛 UnicodeEncodeError 把程序打断——而且 src/* 里的 print 也一并受影响。
# 这里只放宽错误处理、**不改编码**，否则交互式控制台下中文会变成乱码。
# tools/ 与 tests/ 各自已有同样的保护，这里是缺失的入口那一道。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

MISSING_DEP_HELP = """
────────────────────────────────────────────────────────────────────────
  启动失败：缺少运行依赖 {module}

  你当前使用的 Python 是：
        {executable}
        版本 {version}

  请**用同一个解释器**安装依赖（这一点最关键）：

        "{executable}" -m pip install -r "{requirements}"

  机器上装了多个 Python 时（例如 Anaconda 和独立安装的 Python），依赖必须
  装在「你实际用来运行本项目的那一个」里。可以这样确认：

        "{executable}" -m pip list
────────────────────────────────────────────────────────────────────────
"""

try:
    from src.app import TranslatorApp  # noqa: E402
    from src.config import DEFAULT_CONFIG_PATH, Config  # noqa: E402
except ImportError as exc:
    # 多 Python 环境很常见：不要把原始堆栈甩给用户，直接说清装在哪个解释器里
    _module = getattr(exc, "name", None) or str(exc)
    print(
        MISSING_DEP_HELP.format(
            module=_module,
            executable=sys.executable,
            version=sys.version.split()[0],
            requirements=os.path.join(_PROJECT_ROOT, "requirements.txt"),
        )
    )
    raise SystemExit(2) from None

MISSING_KEY_HELP = """
────────────────────────────────────────────────────────────────────────
  还没有配置 DeepSeek API Key，程序无法翻译。

  最省事的方式（交互式填写，会自动写入 config.json）：

         python main.py --setup

  也可以手动配置：

  1) 编辑项目目录下的 config.json，填入：

         {{
           "deepseek": {{
             "api_key": "sk-你的Key"
           }}
         }}

  2) 或者设置环境变量（重启终端后生效）：

         setx DEEPSEEK_API_KEY "sk-你的Key"

  Key 申请地址：https://platform.deepseek.com/api_keys
  配置文件路径：{config_path}

  想先看看界面效果、不消耗额度？运行演示模式：

         python main.py --demo
────────────────────────────────────────────────────────────────────────
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="读取 Windows 实时字幕，用 DeepSeek 实时翻译成中文并显示在置顶浮窗",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "使用前请确认：\n"
            "  1. 按 Win+Ctrl+L 打开 Windows 实时字幕；\n"
            "  2. 在实时字幕的「设置 → 语言」里把语言设为 English；\n"
            "  3. 播放英文视频（声音要能从扬声器输出）。\n\n"
            "浮窗热键：Ctrl+Alt+Q 退出 / T 切换鼠标穿透 / H 显示隐藏 / ↑↓ 移动"
        ),
    )
    parser.add_argument("--config", metavar="路径", help="配置文件路径（默认项目下的 config.json）")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="演示模式：回放内置脚本字幕并使用模拟翻译，不需要 API Key，用来检查界面",
    )
    parser.add_argument("--console", action="store_true", help="不显示浮窗，只在控制台输出中英对照")
    parser.add_argument(
        "--no-click-through",
        action="store_true",
        help="关闭鼠标穿透（默认开启）。关闭后可用鼠标拖动浮窗",
    )
    parser.add_argument("--stable-ms", type=int, metavar="毫秒", help="断句稳定阈值，越大译文越完整但越慢")
    parser.add_argument(
        "--position",
        choices=["bottom-center", "top-center", "bottom-left", "bottom-right"],
        help="浮窗位置",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="交互式填写 / 更换 DeepSeek API Key，写回 config.json 后验证一次",
    )
    parser.add_argument(
        "--check-api",
        action="store_true",
        help="验证 DeepSeek API Key、模型名与网络是否可用（发一次极小的请求），然后退出",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


SETUP_INTRO = """
────────────────────────────────────────────────────────────────────────
  还没有配置 DeepSeek API Key，可以直接在这里填写，不用手动改文件。

    · Key 申请地址：https://platform.deepseek.com/api_keys
    · 输入会被隐藏；粘贴后按回车即可
    · 填完会立即联网校验，通过就自动写入 config.json（保留其他配置项）
────────────────────────────────────────────────────────────────────────
"""


def _probe_api_key(cfg: Config, api_key: str) -> tuple[bool, str]:
    """校验 Key 与模型名。

    实现在 ``src/translator.py``，命令行与浮窗设置面板共用同一份逻辑。
    """
    from src.translator import probe_api_key

    return probe_api_key(
        str(cfg.get("deepseek.base_url", "https://api.deepseek.com")),
        api_key,
        str(cfg.get("deepseek.model")),
    )


def _save_api_key(cfg: Config, api_key: str) -> None:
    """把 Key 合并写回 config.json，保留文件里已有的其他配置项。"""
    from src.config import save_api_key as _save

    _save(cfg.path or DEFAULT_CONFIG_PATH, api_key)


def _prompt_for_api_key_gui(cfg: Config, first_run: bool = True) -> str | None:
    """用图形窗口让用户填写 API Key。

    返回：填好的 Key / 用户取消时的空串 / **无法使用图形界面时的 None**。
    三种结果要分开——"用户取消"不该再去打扰他，"没有图形界面"才需要回退到命令行。
    """
    try:
        from src.settings_dialog import prompt_for_api_key
    except Exception as exc:  # ImportError 或 Tk 初始化失败
        print(f"  [!] 图形设置窗口不可用（{exc}），改用命令行方式。")
        return None
    try:
        return prompt_for_api_key(cfg, first_run=first_run)
    except Exception as exc:
        print(f"  [!] 打不开图形设置窗口（{exc}），改用命令行方式。")
        return None


def _ask_for_api_key(cfg: Config, use_gui: bool, first_run: bool = True) -> str:
    """确保拿到可用的 Key：优先图形窗口，实在不行才回退命令行。"""
    if use_gui:
        got = _prompt_for_api_key_gui(cfg, first_run=first_run)
        if got:
            return got
        if got == "":
            # 用户在窗口里主动取消：不要再弹终端提示
            return ""
    return _prompt_for_api_key(cfg)


def _show_missing_key_message(cfg: Config, use_gui: bool) -> None:
    """提示没有 Key 就没法翻译。图形模式下用窗口提示。

    为什么要用窗口：双击运行时控制台窗口会在程序退出时立刻关闭，
    打印出来的说明用户根本来不及看。
    """
    config_path = cfg.path or DEFAULT_CONFIG_PATH
    if use_gui:
        try:
            from src.settings_dialog import show_message

            show_message(
                "还没有配置 API Key",
                "没有 DeepSeek API Key 就无法翻译。\n\n"
                "申请地址：https://platform.deepseek.com/api_keys\n\n"
                f"填好后保存到：\n{config_path}\n的 deepseek.api_key 字段，再运行本程序即可。",
            )
            return
        except Exception:
            pass
    print(MISSING_KEY_HELP.format(config_path=config_path))


def _prompt_for_api_key(cfg: Config) -> str:
    """交互式引导填写 API Key。返回可用的 Key；用户放弃时返回空串。"""
    import getpass

    print(SETUP_INTRO)
    for attempt in range(1, 4):
        # 有真实终端时隐藏输入；被重定向/在 IDE 控制台里运行时退回可见输入
        prompt = "  请粘贴 API Key（直接回车放弃）: "
        if sys.stdin is not None and sys.stdin.isatty():
            try:
                raw = getpass.getpass(prompt)
            except Exception:
                raw = input(prompt)
        else:
            raw = input(prompt)

        key = (raw or "").strip().strip('"').strip("'")
        if not key:
            return ""
        if not key.startswith("sk-"):
            print("  [!] DeepSeek 的 Key 通常以 sk- 开头。回车重试，输入 y 仍然使用当前值。")
            if input("  > ").strip().lower() != "y":
                continue

        print("  正在联网校验 …")
        ok, message = _probe_api_key(cfg, key)
        print("  " + message)
        if ok:
            cfg.set("deepseek.api_key", key)
            try:
                _save_api_key(cfg, key)
                print(f"  已写入：{cfg.path or DEFAULT_CONFIG_PATH}")
            except OSError as exc:
                print(f"  [!] 写入配置文件失败（{exc}），本次运行仍会使用你输入的 Key。")
            return key
        if attempt < 3:
            print("  请重新输入。")

    print("  已尝试 3 次，放弃。")
    return ""


def _check_api(cfg: Config) -> int:
    """验证 API Key、网络与模型名，然后发一次极小的真实翻译请求。"""
    import requests

    from src.translator import RETIRED_MODELS, DeepSeekTranslator, TranslationError

    if not cfg.api_key:
        print(MISSING_KEY_HELP.format(config_path=cfg.path or DEFAULT_CONFIG_PATH))
        return 2

    base = str(cfg.get("deepseek.base_url", "https://api.deepseek.com")).rstrip("/")
    model = str(cfg.get("deepseek.model"))
    print("正在验证 DeepSeek 接口 …")
    print(f"  接口地址：{base}")
    print(f"  模型    ：{model}")

    # 先做本地检查：已退役的模型名联网也必然失败，不如立刻说清楚
    if model in RETIRED_MODELS:
        print(f"\n[失败] 模型 {model!r} 已于 2026-07-24 完全停用，请求会直接失败。")
        print(f"  请把 config.json 里的 deepseek.model 改为 {RETIRED_MODELS[model]!r}。")
        return 1

    # 第一步：用 /models 同时确认 Key 有效、以及配置的模型名确实可用。
    # 这能把「Key 错」和「模型名错」两类问题分开，避免对着一个笼统的报错猜。
    try:
        listing = requests.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"\n[失败] 无法连接 {base}/models：{exc}")
        print("  请检查网络、代理，以及 config.json 里的 deepseek.base_url。")
        return 1

    if listing.status_code != 200:
        detail = ""
        try:
            detail = str(listing.json().get("error", {}).get("message", ""))[:200]
        except Exception:
            detail = (listing.text or "")[:200]
        hint = {
            401: "API Key 无效或已失效，请检查 config.json 里的 deepseek.api_key。",
            402: "账户余额不足，请先充值。",
        }.get(listing.status_code, "")
        print(f"\n[失败] GET /models 返回 HTTP {listing.status_code}。{hint} {detail}".strip())
        return 1

    try:
        available = [str(item.get("id")) for item in listing.json().get("data", [])]
    except Exception:
        available = []
    if available:
        print(f"  账号可用模型：{', '.join(available)}")
        if model not in available:
            print(f"\n[警告] 配置的模型 {model!r} 不在可用列表里，请改 config.json。")
            return 1

    # 第二步：真实翻译一次，验证完整链路（含 SSE 流式解析）。
    print("\n正在发送一次极小的翻译请求 …")
    translator = DeepSeekTranslator(cfg)
    started = time.monotonic()
    try:
        result = translator.translate("Hello, this is a connectivity test.")
    except TranslationError as exc:
        print(f"\n[失败] {exc}")
        return 1
    finally:
        translator.close()

    print(f"\n[成功] 耗时 {time.monotonic() - started:.2f}s")
    print(f"  译文：{result}")
    print("\n一切正常，可以运行:  python main.py")
    return 0


def _install_shutdown_handler(app: TranslatorApp) -> None:
    """让 Ctrl+C / Ctrl+Break 也能干净退出。

    浮窗模式下主线程阻塞在 Tk 的 ``mainloop``（C 代码）里，``KeyboardInterrupt``
    只会在某个 ``after`` 回调中冒出来，容易被 Tk 吞掉或打印难看的堆栈。
    这里改成直接请求退出：置停止标志 + 关闭浮窗，两种模式都能正常收尾。
    """

    def handler(signum, _frame):
        print("\n收到中断信号，正在退出…")
        app.request_quit()

    for name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config.load(args.config)
    if args.console:
        cfg.set("overlay.enabled", False)
    if args.no_click_through:
        cfg.set("overlay.click_through", False)
    if args.stable_ms:
        cfg.set("captions.stable_ms", max(100, int(args.stable_ms)))
    if args.position:
        cfg.set("overlay.position", args.position)

    if args.setup:
        if not _ask_for_api_key(cfg, use_gui=not args.console, first_run=True):
            _show_missing_key_message(cfg, use_gui=not args.console)
            return 2
        print("\n接下来跑一遍完整链路验证 …")
        return _check_api(cfg)

    if args.check_api:
        if not cfg.api_key and not _ask_for_api_key(cfg, use_gui=not args.console, first_run=False):
            _show_missing_key_message(cfg, use_gui=not args.console)
            return 2
        return _check_api(cfg)

    if not args.demo and not cfg.api_key:
        # 没配 Key 就弹窗口引导填写，而不是只丢一段说明让用户自己去改文件
        if not _ask_for_api_key(cfg, use_gui=not args.console, first_run=True):
            _show_missing_key_message(cfg, use_gui=not args.console)
            return 2
        print()

    app = TranslatorApp(cfg, mock=args.demo, demo=args.demo)
    _install_shutdown_handler(app)
    try:
        return app.run()
    except KeyboardInterrupt:
        app.request_quit()
        print("\n已退出。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

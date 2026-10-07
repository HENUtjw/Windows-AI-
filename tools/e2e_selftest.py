#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""整条链路的自检：**真实 app + 真实浮窗 + 真实 HTTP**，只把 DeepSeek 换成本地 mock。

为什么需要
----------
单元测试各自验证一块，但真正容易出问题的是**把它们接起来**：线程之间会不会互相
饿死、浮窗会不会收不到内容、流式更新会不会闪烁、退出时会不会卡住。

本工具不消耗 API 额度、不需要英文语音、不需要真的开实时字幕，就能把
「采集 → 断句 → HTTP 翻译（含 SSE 流式）→ 浮窗显示」整条链路跑通并断言结果。
Windows 更新后也可以用它快速确认没有被改坏。

运行::

    python tools/e2e_selftest.py
    python tools/e2e_selftest.py --shot overlay.png   # 顺便保存浮窗截图
    python tools/e2e_selftest.py --console            # 只看控制台，不开浮窗
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from mock_deepseek import MockDeepSeekServer  # noqa: E402
from capture_captions import DEFAULT_SPEECH, speak_async  # noqa: E402

import uia_bootstrap  # noqa: E402,F401  (导入即生效，必须早于 uiautomation)

from src.app import TranslatorApp  # noqa: E402
from src.config import Config  # noqa: E402

ECHO_PREFIX = "【自检译】"

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}   {detail}")
        FAILURES.append(name)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
class _NullServer:
    """``--live`` 模式的占位：不启本地 mock，直接打真实 DeepSeek 服务。

    只需满足 run_selftest 用到的那几个属性，避免为了一次开关而重构整个结构。
    """

    base_url = ""
    requests: list = []
    request_count = 0

    def set_scene(self, **kwargs) -> None:
        pass

    def reset_requests(self) -> None:
        pass

    def __enter__(self) -> "_NullServer":
        return self

    def __exit__(self, *exc) -> None:
        return None


def capture_window(hwnd: int, path: str) -> tuple[int, int]:
    """统一实现在 tools/win_capture.py（避免多份 ctypes 代码各踩各的坑）。"""
    from win_capture import capture_window as _capture

    return _capture(hwnd, path)


# --------------------------------------------------------------------------- #
def run_selftest(args) -> int:
    server = _NullServer() if args.live else MockDeepSeekServer()
    with server:
        if not args.live:
            server.set_scene(echo=True, echo_prefix=ECHO_PREFIX)
            if args.fail_first > 0:
                # 前 N 次请求返回 503，用于验证「翻译失败后能否恢复」
                server.set_scene(
                    echo=True, echo_prefix=ECHO_PREFIX,
                    fail_first_n=args.fail_first, fail_status=503,
                )
            print(f"[mock] DeepSeek 替身已启动：{server.base_url}")

        cfg = Config.load(args.config)
        if args.live:
            if not cfg.api_key:
                print("--live 需要 config.json 里配置有效的 deepseek.api_key")
                return 2
            print(f"[live] 使用真实 DeepSeek 服务，模型 {cfg.get('deepseek.model')}")
        else:
            cfg.set("deepseek.api_key", "sk-selftest")
            cfg.set("deepseek.base_url", server.base_url)
        cfg.set("deepseek.stream", True)
        cfg.set("deepseek.max_retries", 1)
        # 推测翻译只在「真实服务」场景里开启并专门验证；替身与混沌场景关掉它，
        # 这样请求数、失败提示等断言才能确定地核对。
        if not args.live or args.fail_first > 0:
            cfg.set("translate.speculate", False)
        cfg.set("captions.log_raw_text", False)
        cfg.set("captions.stable_ms", 1400)
        cfg.set("demo.speed", 2.0)
        cfg.set("overlay.position", "top-center")
        cfg.set("overlay.max_sentences", 2)
        if args.console:
            cfg.set("overlay.enabled", False)

        # demo=True → 脚本化字幕源；demo=False → 真实实时字幕。
        # 两种模式都用真实的 DeepSeekTranslator（指向本地替身），所以 HTTP/SSE 是真跑的。
        app = TranslatorApp(cfg, mock=False, demo=not args.real)

        # DisplayModel 只保留最后若干条，统计「一共提交了多少句」不能看它，
        # 所以这里直接记录每一次提交与每一次错误。
        submitted: list[str] = []
        submitted_at: list[float] = []
        errors_seen: list[str] = []
        original_add = app._display.add
        original_set = app._display.set_translation

        def recording_add(source: str) -> int:
            submitted.append(source)
            submitted_at.append(time.monotonic())
            return original_add(source)

        def recording_set(row_id: int, text: str, done: bool) -> None:
            if done and text.startswith("⚠"):
                errors_seen.append(text)
            return original_set(row_id, text, done)

        app._display.add = recording_add  # type: ignore[assignment]
        app._display.set_translation = recording_set  # type: ignore[assignment]

        # 记录每次翻译请求携带的上文条数。mock 模式可以从服务器端看请求体，
        # 但 --live 模式看不到，所以直接在调用处记录——两种模式都能验证。
        context_sizes: list[int] = []
        original_translate = app._translator.translate

        def recording_translate(text, context=(), on_delta=None):
            context_sizes.append(len(context))
            return original_translate(text, context, on_delta)

        app._translator.translate = recording_translate  # type: ignore[assignment]

        # 真实字幕模式下，用 Windows 语音合成制造英文语音（无人值守也能验证）
        speaker = None
        if args.real and args.speak:
            speaker = speak_async(DEFAULT_SPEECH)
            print("[speak] 已开始播放英文语音")

        result: dict = {}

        def watcher() -> None:
            try:
                time.sleep(args.wait)
                rows = [r for r in app._display.recent(50) if r.done]
                result["rows"] = rows
                result["submitted"] = list(submitted)
                result["submitted_at"] = list(submitted_at)
                result["errors_seen"] = list(errors_seen)
                result["context_sizes"] = list(context_sizes)
                result["speculation_fired"] = app.speculation_fired
                result["speculation_hits"] = app.speculation_hits
                result["added"] = app._display.total_added
                result["requests"] = server.request_count
                result["messages"] = [len(r["body"].get("messages", [])) for r in server.requests]
                if app._overlay is not None:
                    hwnd = app._overlay._hwnd()
                    result["exstyle"] = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
                    result["geometry"] = app._overlay._root.winfo_geometry()
                    if args.shot:
                        result["shot"] = capture_window(hwnd, args.shot)
                    # 浮窗里实际显示的文本
                    result["visible"] = [
                        (s.cget("text"), t.cget("text"))
                        for s, t in app._overlay._rows
                    ]
                    result["status"] = app._overlay._status.cget("text")
            except Exception as exc:  # 自检本身出错也要把进程收干净
                import traceback

                traceback.print_exc()
                result["error"] = repr(exc)
            finally:
                app.request_quit()

        threading.Thread(target=watcher, daemon=True).start()

        # 混沌注入：中途关掉实时字幕，验证程序能否自动重连并继续工作
        if args.kill_at > 0:
            def chaos() -> None:
                time.sleep(args.kill_at)
                print(f"\n[chaos] t+{args.kill_at:.0f}s 关闭实时字幕，验证自动重连 …")
                subprocess.run(
                    ["taskkill", "/IM", "LiveCaptions.exe", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                result["killed_at"] = time.monotonic()

            threading.Thread(target=chaos, daemon=True).start()

        t0 = time.monotonic()
        exit_code = app.run()
        result["t0"] = t0

        if speaker is not None:
            try:
                speaker.wait(timeout=5)
            except Exception:
                speaker.kill()

    # ------------------------------------------------------------------ #
    print("\n" + "=" * 74)
    print("链路自检结果")
    print("=" * 74)

    if "error" in result:
        check("自检过程无异常", False, result["error"])
        return 1
    check("app.run() 正常返回", exit_code == 0, str(exit_code))

    rows = result.get("rows", [])
    submitted = result.get("submitted", [])
    if submitted:
        print("\n本次提交翻译的句子：")
        for index, source in enumerate(submitted, 1):
            print(f"  {index:2d}. {source}")

    check("至少定稿并翻译了 4 句", len(submitted) >= 4, f"只有 {len(submitted)} 句")
    check("每句都有非空译文", all(r.translation.strip() for r in rows), str([r.translation for r in rows]))
    short = [s for s in submitted if len(s.split()) < 3]
    check(
        "几乎没有碎片（至多 2 句过短）",
        len(short) <= 2,
        f"{short}（实时字幕偶尔会在句中虚假断句，属识别侧行为）",
    )
    check(
        "没有超长连缀句（run-on）",
        all(len(s) <= 160 for s in submitted),
        str([len(s) for s in submitted]),
    )
    # 提速取舍：stable_ms=1400 时多数句子会在实时字幕补上标点之前就定稿，
    # 句末标点不再是常态（被强杀时更是如此）。所以只作信息展示，不做断言——
    # 真正的质量把关是「无碎片」和「无超长连缀句」两项。
    punctuated = sum(1 for s in submitted if s.strip()[-1:] in ".!?…。！？")
    print(f"\n  带句末标点的句子：{punctuated}/{len(submitted)}（提速取舍，非硬性要求）")
    if args.live:
        # 真实服务：译文应该是中文，而不是原文回显
        print("\n--- 真实服务翻译结果 ---")
        for index, row in enumerate(rows, 1):
            print(f"  {index}. {row.source}\n     → {row.translation}")
        cjk = [r for r in rows if any("\u4e00" <= ch <= "\u9fff" for ch in r.translation)]
        check("译文包含中文", len(cjk) >= max(1, len(rows) - 1), f"{len(cjk)}/{len(rows)}")
        check(
            "译文不是原文照抄",
            all(r.translation.strip() and r.translation.strip() != r.source.strip() for r in rows),
            str([(r.source[:20], r.translation[:20]) for r in rows[:2]]),
        )
        check(
            "没有出现 API 错误提示",
            not errors_seen,
            str(errors_seen[:2]),
        )
        fired = result.get("speculation_fired", 0)
        hits = result.get("speculation_hits", 0)
        print(f"\n  推测翻译：触发 {fired} 次，命中 {hits} 次（命中即省下一次往返）")
        check(
            "推测翻译确实生效（译文在定稿时就绪）",
            hits >= 1,
            f"触发 {fired} / 命中 {hits}",
        )
    else:
        # 失败行显示的是 ⚠ 提示，本来就没有回显译文，所以只校验成功行
        echo_rows = [r for r in rows if r.translation.startswith(ECHO_PREFIX)]
        check(
            "译文与原文一一对应（回显校验）",
            bool(echo_rows) and all(r.source in r.translation for r in echo_rows),
            str([(r.source[:24], r.translation[:24]) for r in echo_rows[:2]]),
        )
        check("确实发出了 HTTP 请求", result.get("requests", 0) >= 4, str(result.get("requests")))
        # 失败会触发退避重试，所以请求数可以多于提交句数，但不该出现「重复翻译」
        attempts = 2 if args.fail_first > 0 else 1
        check(
            "请求数合理（失败会重试，但没有重复翻译）",
            len(submitted) <= result.get("requests", 0) <= len(submitted) * attempts,
            f"请求 {result.get('requests')} vs 提交 {len(submitted)}",
        )
    check(
        "后续请求携带了上文",
        max(result.get("context_sizes") or [0]) > 0,
        str(result.get("context_sizes")),
    )

    if not args.console:
        ex = result.get("exstyle", 0)
        check("浮窗已创建并有几何尺寸", bool(result.get("geometry")), str(result.get("geometry")))
        click_through = bool(cfg.get("overlay.click_through", False))
        check(
            f"鼠标穿透样式与配置一致（click_through={click_through}）",
            bool(ex & 0x20) == click_through,
            hex(ex),
        )
        check("不抢焦点样式生效 (WS_EX_NOACTIVATE)", bool(ex & 0x08000000), hex(ex))
        check("置顶样式生效 (WS_EX_TOPMOST)", bool(ex & 0x08), hex(ex))
        visible = result.get("visible") or []
        shown = [t for _, t in visible if t]
        check("浮窗上确实显示了译文", bool(shown), str(visible))
        # 不依赖 mock：只要求浮窗显示的内容确实来自产出的译文
        produced_translations = [r.translation for r in rows]
        check(
            "浮窗显示的内容来自最新译文",
            bool(shown) and all(t in produced_translations for t in shown),
            f"显示 {shown}",
        )
        if args.shot:
            check("截图已保存", bool(result.get("shot")), str(result.get("shot")))
            print(f"\n  截图：{args.shot}  尺寸 {result.get('shot')}")

    # ------------------------------------------------------------------ #
    # 混沌场景的专项断言
    # ------------------------------------------------------------------ #
    if args.fail_first > 0:
        print("\n--- 翻译失败恢复 ---")
        check("失败被显示在浮窗里（带 ⚠ 标记）", bool(errors_seen), str(errors_seen[:1]))
        check(
            "失败之后恢复了正常翻译",
            any(r.translation.startswith(ECHO_PREFIX) for r in rows),
            str([r.translation[:30] for r in rows]),
        )
        check("翻译失败没有拖垮程序", exit_code == 0, str(exit_code))
        check(
            "失败的句子没有丢失（请求数不少于提交句数）",
            result.get("requests", 0) >= len(submitted),
            f"请求 {result.get('requests')} vs 提交 {len(submitted)}",
        )

    if args.kill_at > 0:
        print("\n--- 实时字幕中断恢复 ---")
        killed = result.get("killed_at")
        times = result.get("submitted_at") or []
        t0 = result.get("t0") or 0.0
        offsets = [round(t - t0, 1) for t in times]
        before = [t for t in times if killed and t < killed - 1.0]
        after = [t for t in times if killed and t > killed + 4.0]
        check("实时字幕被关闭后程序没有崩", exit_code == 0, str(exit_code))
        check("关闭前有正常产出", len(before) >= 1, f"关闭前 {len(before)} 句 / 全部时间点 {offsets}")
        check(
            "关闭后自动重连并继续产出",
            len(after) >= 1,
            f"关闭后 {len(after)} 句 / 全部时间点 {offsets}",
        )

    # ── 三层翻译链路：短语包 → 缓存 → API ───────────────────── #
    # 注意：主 app 的 _shutdown() 已经干掉了 worker；要测这两条通路，
    # 得另起一个 mock 模式的 TranslatorApp，自带 worker。
    from src.app import TranslatorApp as _CacheProbeApp, _spec_key
    probe_cfg = Config()
    probe_cfg.set("overlay.enabled", False)
    probe_cfg.set("deepseek.api_key", "mock")
    probe_cfg.set("captions.log_raw_text", False)
    probe_app = _CacheProbeApp(probe_cfg, mock=True)
    probe_app._start_threads()
    try:
        # 第一层：内置短语包。整句命中就不该进队列、也不该产生 API 调用。
        before_jobs = probe_app._jobs.qsize()
        probe_app._submit("Yeah,")
        check(
            "phrasebook_hit_skips_queue",
            probe_app._jobs.qsize() == before_jobs,
            f"队列 {before_jobs} -> {probe_app._jobs.qsize()}",
        )
        check("phrasebook_hit_counted", probe_app.phrasebook_hits >= 1, f"{probe_app.phrasebook_hits}")

        # 第二层：AI 学到的缓存。要用**短语包之外**的短句才测得到，
        # 否则会被第一层截胡，observe() 永远不会被调用。
        phrase = "Sounds good to me."
        check(
            "probe_phrase_outside_phrasebook",
            probe_app._phrasebook.lookup(_spec_key(phrase)) is None,
            "探测句不能同时被短语包覆盖",
        )
        # 短暂间隔投递：让 mock 翻译能在两次 _submit 之间完成 observe，
        # 否则多次 _submit 都在 cache 被填充之前完成 lookup，全是 miss。
        for _ in range(7):
            probe_app._submit(phrase)
            time.sleep(0.12)
        time.sleep(1.2)                  # 让 mock (sleep 50ms) + observe 落定
        check(
            "cache_size_after_repeats",
            probe_app._cache.size() >= 1,
            f"size={probe_app._cache.size()}",
        )
        check(
            "cache_hits_after_repeats",
            probe_app.cache_hits >= 1,
            f"hits={probe_app.cache_hits}",
        )
        check(
            "cache_misses_strict_increase",
            probe_app.cache_misses >= 1,
            f"misses={probe_app.cache_misses}",
        )
    finally:
        probe_app._shutdown()

    print("\n" + "=" * 74)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for name in FAILURES:
            print(f"  · {name}")
        return 1
    print("整条链路自检通过 (ALL PASS)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端链路自检（DeepSeek 由本地 mock 顶替）")
    parser.add_argument("--config", help="配置文件路径（默认项目下的 config.json）")
    parser.add_argument("--console", action="store_true", help="不开浮窗，只跑控制台输出")
    parser.add_argument("--shot", metavar="路径", help="把浮窗内容截图保存到指定文件")
    parser.add_argument(
        "--real",
        action="store_true",
        help="用真实实时字幕（需已把识别语言设为英语）而不是脚本化字幕",
    )
    parser.add_argument("--speak", action="store_true", help="配合 --real：用语音合成播英文，无人值守也能验证")
    parser.add_argument("--wait", type=float, default=15.0, help="抓取时长（秒），--real 时建议 40")
    parser.add_argument(
        "--kill-at",
        type=float,
        default=0.0,
        metavar="秒",
        help="混沌注入：运行到第 N 秒时强制关闭实时字幕，验证能否自动重连（需配合 --real）",
    )
    parser.add_argument(
        "--fail-first",
        type=int,
        default=0,
        metavar="次数",
        help="混沌注入：让 DeepSeek 替身的前 N 次请求返回 503，验证翻译失败后能否恢复",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="使用 config.json 里配置的真实 DeepSeek 服务（会消耗额度），而不是本地替身",
    )
    args = parser.parse_args()
    return run_selftest(args)


if __name__ == "__main__":
    raise SystemExit(main())
